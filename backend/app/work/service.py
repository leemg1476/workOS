"""Transactional work kernel. PostgreSQL is the sole source of official state."""
from uuid import uuid4
from sqlalchemy import text
from app.domain.contracts import WorkStatus, TRANSITIONS
from app.integrations.artifact_store import store_json
from app.domain.hashing import canonical, digest


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def uid():
    return str(uuid4())


def one(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).mappings().first()


class Kernel:
    def __init__(self, engine):
        self.engine = engine

    def _lock(self, conn):
        # Serializes graph mutations, state changes, and queue claims across processes.
        conn.execute(text('SELECT pg_advisory_xact_lock(947202)'))

    def _event(self, conn, kind, subject, payload, actor='kernel', cause=None, prior=None, key=None):
        conn.execute(text('''INSERT INTO work_events(event_id,event_type,subject_id,causation_id,actor,prior_version,idempotency_key,payload)
            VALUES (:id,:kind,:subject,:cause,:actor,:prior,:key,CAST(:payload AS jsonb))'''),
            dict(id=uid(), kind=kind, subject=str(subject), cause=cause, actor=actor, prior=prior, key=key, payload=canonical(payload)))

    def _work(self, conn, work_id):
        row = one(conn, 'SELECT * FROM work_items WHERE work_id=:id FOR UPDATE', {'id': work_id})
        if not row:
            raise NotFound('work item not found')
        return row

    def _transition(self, conn, work_id, target, cause=None):
        work = self._work(conn, work_id)
        old, new = WorkStatus(work['status']), WorkStatus(target)
        if new not in TRANSITIONS.get(old, set()):
            raise Conflict(f'invalid transition {old.value} -> {new.value}')
        conn.execute(text('UPDATE work_items SET status=:status,version=version+1 WHERE work_id=:id'), {'status': new.value, 'id': work_id})
        self._event(conn, 'work.transition', work_id, {'from': old.value, 'to': new.value}, cause=cause, prior=work['version'])








    def trigger(self, job_id, key, snapshot, *, expected_due=None):
        with self.engine.begin() as c:
            self._lock(c)
            existing = one(c, 'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key', {'key': key})
            if existing:
                if existing['payload'] != {'job_id': job_id, 'input_snapshot': snapshot}:
                    raise Conflict('idempotency key already used for a different request')
                return existing['subject_id'], False
            job = one(c, 'SELECT * FROM job_definitions WHERE job_id=:id AND enabled', {'id': job_id})
            if not job:
                raise NotFound('enabled job not found')
            if expected_due is not None:
                cursor = one(c, 'SELECT next_due_at FROM job_schedule_cursors WHERE job_id=:id', {'id':job_id})
                if not cursor or cursor['next_due_at'] != expected_due:
                    return None, False
            work_id = uid()
            c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status)
                VALUES (:id,:job,:task,:version,CAST(:snapshot AS jsonb),'CREATED')'''),
                dict(id=work_id, job=job_id, task=job['task_id'], version=job['task_version'], snapshot=canonical(snapshot)))
            from app.workloads.registry import load_configured, trigger_hooks
            load_configured()
            task = one(c, 'SELECT acceptance_policy FROM task_definitions WHERE task_id=:id AND version=:version',
                       {'id':job['task_id'],'version':job['task_version']})
            hook = trigger_hooks.get(task['acceptance_policy'].get('kind'))
            if hook:
                hook(self,c,dict(job),work_id)
            self._event(c, 'work.created', work_id, {'job_id': job_id, 'input_snapshot': snapshot}, actor='user', key=key)
            self._transition(c, work_id, 'READY', key)
            c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"), {'id': work_id})
            return work_id, True

    def claim(self, lease_seconds=60):
        with self.engine.begin() as c:
            self._lock(c)
            # A dead worker cannot leave a RUNNING attempt permanently claimed.
            expired = c.execute(text("SELECT * FROM work_queue WHERE status='CLAIMED' AND lease_until < now() FOR UPDATE")).mappings().all()
            for item in expired:
                c.execute(text("UPDATE runs SET status='FAILED',error='worker lease expired',finished_at=now() WHERE run_id=:id AND status='RUNNING'"), {'id': item['run_id']})
                self._transition(c, item['work_id'], 'REWORK_REQUIRED', str(item['run_id']))
                self._transition(c, item['work_id'], 'READY', str(item['run_id']))
                self._event(c, 'run.failed', item['run_id'], {'reason': 'worker lease expired'})
                c.execute(text("UPDATE work_queue SET status='PENDING',run_id=NULL,lease_until=NULL,notified_at=NULL WHERE work_id=:id"), {'id': item['work_id']})
            item = one(c, """SELECT q.* FROM work_queue q JOIN work_items w USING(work_id)
                WHERE q.status='PENDING' AND q.available_at <= now() AND w.status='READY'
                AND NOT EXISTS (SELECT 1 FROM work_dependencies d JOIN work_items p ON p.work_id=d.predecessor_id
                  WHERE d.successor_id=w.work_id AND (p.status <> 'COMMITTED' OR p.accepted_artifact_id IS NULL
                    OR (d.pinned_artifact_version IS NOT NULL AND d.pinned_artifact_version <> p.accepted_artifact_id)))
                ORDER BY w.priority DESC,q.available_at FOR UPDATE OF q SKIP LOCKED LIMIT 1""")
            if not item:
                return None
            pins = c.execute(text('''UPDATE work_dependencies d SET pinned_artifact_version=p.accepted_artifact_id
                FROM work_items p WHERE d.predecessor_id=p.work_id AND d.successor_id=:id AND d.pinned_artifact_version IS NULL
                RETURNING d.predecessor_id,d.pinned_artifact_version'''), {'id': item['work_id']}).mappings().all()
            for pin in pins:
                self._event(c, 'dependency.pinned', item['work_id'], {'predecessor_id': str(pin['predecessor_id']), 'artifact_id': str(pin['pinned_artifact_version'])})
            run_id = uid()
            attempt = one(c, 'SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM runs WHERE work_id=:id', {'id': item['work_id']})['n']
            c.execute(text("INSERT INTO runs(run_id,work_id,attempt_no,status,agent_version,model_id,prompt_version) VALUES (:run,:work,:n,'RUNNING','kernel-mock-v1','MOCK','none')"), {'run': run_id, 'work': item['work_id'], 'n': attempt})
            self._transition(c, item['work_id'], 'RUNNING', run_id)
            c.execute(text("UPDATE work_queue SET status='CLAIMED',run_id=:run,lease_until=now()+(:seconds * interval '1 second') WHERE work_id=:work"), {'run': run_id, 'work': item['work_id'], 'seconds': lease_seconds})
            self._event(c, 'run.started', run_id, {'work_id': str(item['work_id']), 'attempt_no': attempt})
            work = self._work(c, item['work_id'])
            from app.work.task_memory import pin
            pin(self, c, run_id, work)
            return {'work_id': str(item['work_id']), 'run_id': run_id, 'attempt_no': attempt,
                    'task_id':work['task_id'], 'task_version':work['task_version'], 'input_snapshot':work['input_snapshot']}

    def fail(self, run_id, reason, *, retry_delay_seconds=None, max_attempts=3):
        with self.engine.begin() as c:
            self._lock(c)
            run = one(c, "SELECT r.* FROM runs r JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until > now() FOR UPDATE OF r", {'id': run_id})
            if not run:
                raise Conflict('run is not running')
            c.execute(text("UPDATE runs SET status='FAILED',error=:error,finished_at=now() WHERE run_id=:id"), {'error': reason, 'id': run_id})
            self._transition(c, run['work_id'], 'REWORK_REQUIRED', run_id)
            c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id': run['work_id']})
            self._event(c, 'run.failed', run_id, {'reason': reason})
            if retry_delay_seconds is not None and run['attempt_no'] < max_attempts:
                self._transition(c, run['work_id'], 'READY', run_id)
                c.execute(text("""UPDATE work_queue SET status='PENDING',run_id=NULL,lease_until=NULL,
                    notified_at=NULL,available_at=now()+(:delay * interval '1 second') WHERE work_id=:id"""),
                    {'id':run['work_id'],'delay':max(0,retry_delay_seconds)})
                self._event(c, 'run.retry_scheduled', run_id, {'work_id':str(run['work_id']),
                    'attempt_no':run['attempt_no'],'max_attempts':max_attempts,'delay_seconds':max(0,retry_delay_seconds)})

    def retry(self, work_id, key):
        with self.engine.begin() as c:
            self._lock(c)
            previous = one(c, 'SELECT subject_id,event_type FROM work_events WHERE idempotency_key=:key', {'key': key})
            if previous:
                if previous['subject_id'] != work_id or previous['event_type'] != 'work.retry':
                    raise Conflict('idempotency key already used')
                return False
            work = self._work(c, work_id)
            from app.workloads.registry import load_configured, retry_guards
            load_configured()
            task=one(c,'SELECT acceptance_policy FROM task_definitions WHERE task_id=:id AND version=:version',
                     {'id':work['task_id'],'version':work['task_version']})
            guard=retry_guards.get(task['acceptance_policy'].get('kind'))
            if guard:
                guard(c,dict(work))
            if work['status'] in ('STALE', 'BLOCKED', 'REWORK_REQUIRED'):
                unavailable = one(c, """SELECT 1 FROM work_dependencies d JOIN work_items p ON p.work_id=d.predecessor_id
                    WHERE d.successor_id=:id AND (p.status <> 'COMMITTED' OR p.accepted_artifact_id IS NULL) LIMIT 1""", {'id': work_id})
                if unavailable:
                    raise Conflict('dependency is not committed')
                pins = c.execute(text('''UPDATE work_dependencies d SET pinned_artifact_version=p.accepted_artifact_id FROM work_items p
                    WHERE d.predecessor_id=p.work_id AND d.successor_id=:id
                    RETURNING d.predecessor_id,d.pinned_artifact_version'''), {'id': work_id}).mappings().all()
                self._event(c, 'dependency.revalidated', work_id, {'pins': [{'predecessor_id': str(pin['predecessor_id']), 'artifact_id': str(pin['pinned_artifact_version'])} for pin in pins]}, actor='user', cause=key)
            self._transition(c, work_id, 'READY', key)
            c.execute(text("""INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING') ON CONFLICT(work_id)
                DO UPDATE SET status='PENDING',available_at=now(),run_id=NULL,lease_until=NULL,notified_at=NULL"""), {'id':work_id})
            self._event(c, 'work.retry', work_id, {}, actor='user', key=key)
            return True

    def add_dependency(self, predecessor, successor, kind='artifact'):
        with self.engine.begin() as c:
            self._lock(c)
            self._add_dependency(c,predecessor,successor,kind)

    def _add_dependency(self, c, predecessor, successor, kind='artifact'):
        """Add a dependency in the caller's locked transaction."""
        before, after = self._work(c, predecessor), self._work(c, successor)
        if after['status'] not in ('CREATED', 'READY', 'BLOCKED'):
            raise Conflict('dependencies can only be added before execution')
        if before['work_id'] == after['work_id']:
            raise Conflict('dependency cycle')
        cycle = one(c, '''WITH RECURSIVE descendants(id) AS (
            SELECT successor_id FROM work_dependencies WHERE predecessor_id=:successor
            UNION SELECT d.successor_id FROM work_dependencies d JOIN descendants x ON d.predecessor_id=x.id)
            SELECT 1 FROM descendants WHERE id=:predecessor''', {'successor': successor, 'predecessor': predecessor})
        if cycle:
            raise Conflict('dependency cycle')
        if one(c, 'SELECT 1 FROM work_dependencies WHERE predecessor_id=:p AND successor_id=:s', {'p': predecessor, 's': successor}):
            raise Conflict('dependency already exists')
        c.execute(text('INSERT INTO work_dependencies VALUES (:p,:s,:kind,:pin)'), {'p': predecessor, 's': successor, 'kind': kind, 'pin': before['accepted_artifact_id']})
        self._event(c, 'dependency.created', successor, {'predecessor_id': str(predecessor), 'kind': kind, 'pinned_artifact_id': str(before['accepted_artifact_id']) if before['accepted_artifact_id'] else None})

    def stale_descendants(self, c, work_id, cause):
        rows = c.execute(text('''WITH RECURSIVE descendants(id) AS (
            SELECT successor_id FROM work_dependencies WHERE predecessor_id=:id
            UNION SELECT d.successor_id FROM work_dependencies d JOIN descendants x ON d.predecessor_id=x.id)
            SELECT w.work_id,w.status FROM work_items w JOIN descendants d ON w.work_id=d.id'''), {'id': work_id}).mappings().all()
        for row in rows:
            if row['status'] == 'COMMITTED':
                self._transition(c, row['work_id'], 'STALE', cause)
            elif row['status'] == 'READY':
                self._transition(c, row['work_id'], 'BLOCKED', cause)
            elif row['status'] == 'RUNNING':
                active = one(c, "SELECT run_id FROM runs WHERE work_id=:id AND status='RUNNING'", {'id': row['work_id']})
                if active:
                    c.execute(text("UPDATE runs SET status='FAILED',error='dependency became stale',finished_at=now() WHERE run_id=:id"), {'id': active['run_id']})
                    c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id': row['work_id']})
                    self._event(c, 'run.failed', active['run_id'], {'reason': 'dependency became stale'}, cause=cause)
                self._transition(c, row['work_id'], 'REWORK_REQUIRED', cause)
            elif row['status'] == 'WAITING_APPROVAL':
                # A human cannot accept a candidate based on superseded dependencies.
                self._transition(c, row['work_id'], 'STALE', cause)

    def invalidate(self, work_id, cause):
        with self.engine.begin() as c:
            self._lock(c)
            self._transition(c, work_id, 'STALE', cause)
            self.stale_descendants(c, work_id, cause)

    def revalidate(self, work_id, key):
        with self.engine.begin() as c:
            self._lock(c)
            event_key = 'work.revalidate:'+key
            prior = one(c,'SELECT subject_id,event_type FROM work_events WHERE idempotency_key=:key',{'key':event_key})
            if prior:
                if prior['subject_id'] != work_id or prior['event_type'] != 'work.revalidate':
                    raise Conflict('idempotency key already used')
                return False
            work = self._work(c,work_id)
            from app.workloads.registry import load_configured, revalidate_guards
            load_configured()
            task=one(c,'SELECT acceptance_policy FROM task_definitions WHERE task_id=:id AND version=:version',
                     {'id':work['task_id'],'version':work['task_version']})
            guard=revalidate_guards.get(task['acceptance_policy'].get('kind'))
            if guard:
                guard(c,dict(work))
            if work['status'] != 'COMMITTED':
                raise Conflict('only committed work can be revalidated')
            unavailable = one(c,"""SELECT 1 FROM work_dependencies d JOIN work_items p ON p.work_id=d.predecessor_id
                WHERE d.successor_id=:id AND (p.status <> 'COMMITTED' OR p.accepted_artifact_id IS NULL) LIMIT 1""", {'id':work_id})
            if unavailable:
                raise Conflict('dependency is not committed')
            pins = c.execute(text('''UPDATE work_dependencies d SET pinned_artifact_version=p.accepted_artifact_id FROM work_items p
                WHERE d.predecessor_id=p.work_id AND d.successor_id=:id RETURNING d.predecessor_id,d.pinned_artifact_version'''), {'id':work_id}).mappings().all()
            if pins:
                self._event(c,'dependency.revalidated',work_id,{'pins':[{'predecessor_id':str(pin['predecessor_id']),
                    'artifact_id':str(pin['pinned_artifact_version'])} for pin in pins]},actor='user',cause=key)
            self._transition(c,work_id,'STALE',key)
            self.stale_descendants(c,work_id,key)
            self._transition(c,work_id,'READY',key)
            c.execute(text("""INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING') ON CONFLICT(work_id)
                DO UPDATE SET status='PENDING',available_at=now(),run_id=NULL,lease_until=NULL,notified_at=NULL"""),{'id':work_id})
            self._event(c,'work.revalidate',work_id,{'previous_artifact_id':str(work['accepted_artifact_id']) if work['accepted_artifact_id'] else None},
                        actor='user',cause=key,prior=work['version'],key=event_key)
            return True
















    def submit(self, run_id, content):
        """Run configured deterministic field validator; failed output is never published."""
        with self.engine.begin() as c:
            self._lock(c)
            run = one(c, "SELECT r.* FROM runs r JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until > now() FOR UPDATE OF r", {'id': run_id})
            if not run:
                raise Conflict('run is not running')
            work = self._work(c, run['work_id'])
            task = one(c, 'SELECT * FROM task_definitions WHERE task_id=:id AND version=:v', {'id': work['task_id'], 'v': work['task_version']})
            kind = task['acceptance_policy'].get('kind')
            from app.workloads.registry import load_builtins, verifiers, verifier_kind_resolvers, acceptance_hooks
            load_builtins()
            resolver = verifier_kind_resolvers.get(kind)
            if resolver:
                kind = resolver(dict(work))
            extension = verifiers.get(kind)
            if extension is None and kind != 'mock_receipt':
                raise Conflict('configured task validator is not implemented')
            artifact_id = uid()
            artifact_kind = extension.artifact_kind if extension else 'mock_receipt'
            artifact_hash = store_json(c, artifact_id, run_id, content, artifact_kind,
                                       schema_version=extension.schema_version if extension else '1')
            self._transition(c, work['work_id'], 'SUBMITTED', run_id)
            self._transition(c, work['work_id'], 'VERIFYING', run_id)
            if extension:
                result, refs = extension.verify(c, dict(run), dict(work), dict(task), content, artifact_hash)
                if not isinstance(result, dict) or type(result.get('passed')) is not bool or not isinstance(refs, dict):
                    raise Conflict('invalid verifier result')
                verifier, verification = extension.verifier_version, extension.verification_type
                c.execute(text('''INSERT INTO evaluations(evaluation_id,run_id,artifact_id,eval_suite_version,metric_results,verdict)
                    VALUES (:id,:run,:artifact,:suite,CAST(:result AS jsonb),:verdict)'''),
                    {'id':uid(),'run':run_id,'artifact':artifact_id,'suite':extension.suite_version,
                     'result':canonical(result),'verdict':'PASS' if result['passed'] else 'FAIL'})
            else:
                required = task['acceptance_policy'].get('required_fields', [])
                missing = [field for field in required if field not in content or content[field] in (None, '')]
                passed = bool(required) and not missing and content.get('mode') == 'MOCK' and content.get('task_id') == work['task_id']
                result = {'passed':passed, 'missing':missing, 'checks':['required_fields','mock_mode','task_id']}
                refs = {'task_id':work['task_id'], 'task_version':work['task_version'], 'sha256':artifact_hash}
                verifier, verification = 'receipt-validator-v1', 'mock_receipt_contract'
            passed = result['passed']
            c.execute(text('''INSERT INTO evidence(evidence_id,artifact_id,verification_type,input_refs,result,verifier_version)
                VALUES (:id,:artifact,:verification,CAST(:refs AS jsonb),CAST(:result AS jsonb),:verifier)'''),
                {'id':uid(), 'artifact':artifact_id, 'verification':verification, 'refs':canonical(refs), 'result':canonical(result), 'verifier':verifier})
            if passed and result.get('requires_human_review') is True:
                self._transition(c, work['work_id'], 'WAITING_APPROVAL', run_id)
            elif passed:
                old = work['accepted_artifact_id']
                self._transition(c, work['work_id'], 'ACCEPTED', run_id)
                c.execute(text('UPDATE work_items SET accepted_artifact_id=:artifact WHERE work_id=:work'), {'artifact': artifact_id, 'work': work['work_id']})
                self._transition(c, work['work_id'], 'COMMITTED', run_id)
                if old and str(old) != artifact_id:
                    self.stale_descendants(c, work['work_id'], run_id)
                hook = acceptance_hooks.get(kind)
                if hook:
                    hook(self,c,dict(work),run_id)
            else:
                self._transition(c, work['work_id'], 'REWORK_REQUIRED', run_id)
            c.execute(text('UPDATE runs SET status=:status,finished_at=now(),error=:error WHERE run_id=:id'), {'status': 'SUCCEEDED' if passed else 'VERIFICATION_FAILED', 'error': None if passed else canonical(result), 'id': run_id})
            c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id': work['work_id']})
            self._event(c, 'run.verified', run_id, {'artifact_id': artifact_id, **result})
            return {'artifact_id': artifact_id, 'passed': passed, 'result': result}

    def review_artifact(self, work_id, artifact_id, decision, note, idempotency_key):
        note = note.strip()
        if not note or decision not in ('ACCEPT','REJECT'):
            raise Conflict('a review decision and note are required')
        key = 'work.review:' + idempotency_key
        payload = {'work_id':str(work_id),'artifact_id':str(artifact_id),'decision':decision,'note':note}
        with self.engine.begin() as c:
            self._lock(c)
            previous = one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
            if previous:
                if previous['payload'] != payload:
                    raise Conflict('review idempotency key already used')
                return False
            work = self._work(c,work_id)
            if work['status'] != 'WAITING_APPROVAL':
                raise Conflict('work is not awaiting review')
            from app.workloads.registry import load_configured, review_guards
            load_configured()
            task=one(c,'SELECT acceptance_policy FROM task_definitions WHERE task_id=:id AND version=:version',
                     {'id':work['task_id'],'version':work['task_version']})
            guard=review_guards.get(task['acceptance_policy'].get('kind'))
            if guard:
                guard(c,dict(work))
            if decision == 'ACCEPT' and not one(c,'SELECT 1 FROM task_definitions WHERE task_id=:task AND version=:version AND active',
                                               {'task':work['task_id'],'version':work['task_version']}):
                raise Conflict('work was executed under an inactive task version')
            if decision == 'ACCEPT' and one(c,'''SELECT 1 FROM work_dependencies d JOIN work_items p ON p.work_id=d.predecessor_id
                WHERE d.successor_id=:id AND (p.status <> 'COMMITTED' OR p.accepted_artifact_id IS NULL
                    OR d.pinned_artifact_version IS NULL OR d.pinned_artifact_version <> p.accepted_artifact_id) LIMIT 1''',
                {'id':work_id}):
                raise Conflict('review target has stale or unpinned dependencies')
            candidate = one(c,'''SELECT a.*,r.status AS run_status,e.result AS verification FROM artifacts a
                JOIN runs r ON r.run_id=a.run_id JOIN evidence e ON e.artifact_id=a.artifact_id
                WHERE a.artifact_id=:artifact AND r.work_id=:work AND e.result->>'requires_human_review'='true'
                  AND r.attempt_no=(SELECT max(attempt_no) FROM runs WHERE work_id=:work)''',
                {'artifact':artifact_id,'work':work_id})
            if not candidate or candidate['run_status']!='SUCCEEDED' or candidate['verification'].get('passed') is not True or digest(candidate['content']) != candidate['sha256']:
                raise Conflict('review target is not current or structurally verified')
            review = {'decision':decision,'note':note,'scope':'HUMAN_JUDGMENT_NOT_INDEPENDENT_FACT_VERIFICATION'}
            c.execute(text('''INSERT INTO evidence(evidence_id,artifact_id,verification_type,input_refs,result,verifier_version)
                VALUES (:id,:artifact,'human_work_review',CAST(:refs AS jsonb),CAST(:result AS jsonb),'human-work-review-v1')'''),
                {'id':uid(),'artifact':artifact_id,'refs':canonical({'artifact_sha256':candidate['sha256']}),'result':canonical(review)})
            if decision == 'ACCEPT':
                old = work['accepted_artifact_id']
                self._transition(c,work_id,'ACCEPTED',str(artifact_id))
                c.execute(text('UPDATE work_items SET accepted_artifact_id=:artifact WHERE work_id=:work'),{'artifact':artifact_id,'work':work_id})
                self._transition(c,work_id,'COMMITTED',str(artifact_id))
                if old and str(old)!=str(artifact_id):
                    self.stale_descendants(c,work_id,str(artifact_id))
                from app.workloads.registry import acceptance_hooks
                hook=acceptance_hooks.get(task['acceptance_policy'].get('kind'))
                if hook:
                    hook(self,c,dict(work),str(candidate['run_id']))
            else:
                self._transition(c,work_id,'REWORK_REQUIRED',str(artifact_id))
            self._event(c,'work.reviewed',work_id,payload,actor='user',cause=str(artifact_id),key=key)
            return True
