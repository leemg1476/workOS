"""Transactional work kernel. PostgreSQL is the sole source of official state."""
import json
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from app.domain.contracts import WorkStatus, TRANSITIONS, PolicyReviewReport
from app.integrations.artifact_store import store_json
from app.work.market import build_manifest, digest, PROMPT_VERSION
from app.work.policy import STEPS, build_policy_manifest
from app.work.voc import DOCUMENTS as VOC_DOCUMENTS, DOCUMENT_QUERY as VOC_DOCUMENT_QUERY, DOCUMENT_PARAMETERS as VOC_DOCUMENT_PARAMETERS, RECORD_QUERY as VOC_RECORD_QUERY
from app.integrations.search import retrieve_documents, demo_index_target, configuration as search_configuration
from app.integrations.sql_source import source_engine, source_mode
from app.evaluation.policy_regression import load_inputs as load_regression_inputs


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def uid():
    return str(uuid4())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


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

    def _schedule_policy_recheck(self, c, root_id, cause):
        from app.workloads.policy_lifecycle import schedule_policy_recheck
        return schedule_policy_recheck(self, c, root_id, cause)

    def _schedule_policy_index(self, c, root_id, cause):
        from app.workloads.policy_lifecycle import schedule_policy_index
        return schedule_policy_index(self, c, root_id, cause)

    def _policy_regression_chain_current(self, c, work_id, allow_unpinned=False):
        from app.workloads.policy_verifiers import policy_regression_chain_current
        return policy_regression_chain_current(c, work_id, allow_unpinned)

    def _schedule_policy_regression(self, c, root_id, cause, *, allow_rework=False):
        from app.workloads.policy_lifecycle import schedule_policy_regression
        return schedule_policy_regression(self, c, root_id, cause, allow_rework=allow_rework)

    def _open_policy_review(self, c, root_id, cause):
        from app.workloads.policy_lifecycle import open_policy_review
        return open_policy_review(self, c, root_id, cause)

    def policy_method_changed(self, c, cause):
        from app.workloads.policy_lifecycle import policy_method_changed
        return policy_method_changed(self, c, cause)

    def recover_policy_followups(self):
        from app.workloads.policy_lifecycle import recover_policy_followups
        return recover_policy_followups(self)

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
            return {'work_id': str(item['work_id']), 'run_id': run_id, 'attempt_no': attempt,
                    'task_id':work['task_id'], 'task_version':work['task_version'], 'input_snapshot':work['input_snapshot']}

    def fail(self, run_id, reason):
        with self.engine.begin() as c:
            self._lock(c)
            run = one(c, "SELECT r.* FROM runs r JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until > now() FOR UPDATE OF r", {'id': run_id})
            if not run:
                raise Conflict('run is not running')
            c.execute(text("UPDATE runs SET status='FAILED',error=:error,finished_at=now() WHERE run_id=:id"), {'error': reason, 'id': run_id})
            self._transition(c, run['work_id'], 'REWORK_REQUIRED', run_id)
            c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id': run['work_id']})
            self._event(c, 'run.failed', run_id, {'reason': reason})

    def retry(self, work_id, key):
        with self.engine.begin() as c:
            self._lock(c)
            previous = one(c, 'SELECT subject_id,event_type FROM work_events WHERE idempotency_key=:key', {'key': key})
            if previous:
                if previous['subject_id'] != work_id or previous['event_type'] != 'work.retry':
                    raise Conflict('idempotency key already used')
                return False
            work = self._work(c, work_id)
            experiment = one(c,'''SELECT x.status FROM text_experiment_work m JOIN text_experiments x ON x.experiment_id=m.experiment_id
                WHERE m.work_id=:id''',{'id':work_id})
            if experiment and experiment['status'] != 'RUNNING':
                raise Conflict('completed experiment work cannot be retried')
            if work['task_id'] == 'TASK-002' and work['parent_work_id'] is not None and work['input_snapshot'].get('step_no') not in (1,2,3):
                raise Conflict('policy follow-up execution is not implemented')
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
            self._event(c, 'dependency.created', successor, {'predecessor_id': predecessor, 'kind': kind, 'pinned_artifact_id': str(before['accepted_artifact_id']) if before['accepted_artifact_id'] else None})

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
            if work['task_id']=='TASK-002' and work['parent_work_id'] is not None and work['input_snapshot'].get('step_no')==4:
                raise Conflict('human review must be reopened explicitly')
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

    def document_updated(self, request):
        """Register a source change, affected tasks and follow-up DAG atomically."""
        data = request.model_dump()
        with self.engine.begin() as c:
            self._lock(c)
            existing = one(c, 'SELECT * FROM policy_change_events WHERE event_id=:id', {'id':data['event_id']})
            if existing:
                if any(existing[k] != data[k] for k in ('document_id','old_version','new_version','old_sha256','new_sha256')):
                    raise Conflict('event ID already used for a different change')
                return str(existing['root_work_id']), False
            same_change = one(c, '''SELECT * FROM policy_change_events WHERE document_id=:document_id
                AND old_version=:old_version AND new_version=:new_version''', data)
            if same_change:
                if same_change['old_sha256'] != data['old_sha256'] or same_change['new_sha256'] != data['new_sha256']:
                    raise Conflict('document version hash mismatch')
                return str(same_change['root_work_id']), False
            if data['old_version'] >= data['new_version']:
                raise Conflict('new document version must be newer')
            docs = c.execute(text('''SELECT version,clauses,sha256 FROM policy_documents WHERE document_id=:document_id
                AND version IN (:old_version,:new_version)'''), data).mappings().all()
            docs = {d['version']:d for d in docs}
            for version, supplied in ((data['old_version'],data['old_sha256']),(data['new_version'],data['new_sha256'])):
                if version not in docs or docs[version]['sha256'] != supplied or digest(docs[version]['clauses']) != supplied:
                    raise Conflict('document version or hash does not match stored source')
            dependencies = c.execute(text('''SELECT task_id,pinned_version FROM task_source_dependencies
                WHERE document_id=:doc AND pinned_version=:version AND status='CURRENT' ORDER BY task_id FOR UPDATE'''),
                {'doc':data['document_id'],'version':data['old_version']}).mappings().all()
            if not dependencies:
                raise Conflict('no current task depends on the old version')
            job = one(c, "SELECT * FROM job_definitions WHERE job_id='TASK-002-JOB' AND enabled")
            if not job or job['task_version'] != 2:
                raise Conflict('policy workflow job is unavailable')
            root = uid()
            def create(work_id, parent, snapshot):
                c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status,parent_work_id)
                    VALUES (:id,:job,:task,:version,CAST(:snapshot AS jsonb),'CREATED',:parent)'''),
                    {'id':work_id,'job':job['job_id'],'task':job['task_id'],'version':job['task_version'],
                     'snapshot':canonical(snapshot),'parent':parent})
                self._event(c,'work.created',work_id,{'job_id':job['job_id'],'input_snapshot':snapshot},actor='webhook',cause=data['event_id'])
            create(root,None,{'source_event':data['event_id'],'document_id':data['document_id'],
                              'old_version':data['old_version'],'new_version':data['new_version']})
            affected = [d['task_id'] for d in dependencies]
            c.execute(text('''INSERT INTO policy_change_events(event_id,document_id,old_version,new_version,old_sha256,new_sha256,affected_tasks,root_work_id)
                VALUES (:event_id,:document_id,:old_version,:new_version,:old_sha256,:new_sha256,CAST(:affected AS jsonb),:root)'''),
                {**data,'affected':canonical(affected),'root':root})
            self._event(c,'document.updated',root,data,actor='webhook',cause=data['event_id'],key='document.updated:'+data['event_id'])
            previous = root
            for index,title in enumerate(STEPS,1):
                child = uid()
                create(child,root,{'source_event':data['event_id'],'step_no':index,'title':title,'requires_human_or_future_worker':True})
                c.execute(text('INSERT INTO policy_followups(root_work_id,work_id,step_no,title) VALUES (:root,:child,:n,:title)'),
                          {'root':root,'child':child,'n':index,'title':title})
                c.execute(text("INSERT INTO work_dependencies(predecessor_id,successor_id,dependency_kind) VALUES (:p,:s,'policy_followup')"),
                          {'p':previous,'s':child})
                self._event(c,'dependency.created',child,{'predecessor_id':previous,'kind':'policy_followup'},cause=data['event_id'])
                self._transition(c,child,'WAITING_APPROVAL' if index == len(STEPS) else 'BLOCKED',data['event_id'])
                previous = child
            self._transition(c,root,'READY',data['event_id'])
            c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"), {'id':root})
            for dep in dependencies:
                c.execute(text("UPDATE task_source_dependencies SET status='REVIEW_REQUIRED' WHERE task_id=:task AND document_id=:doc"),
                          {'task':dep['task_id'],'doc':data['document_id']})
                self._event(c,'task.source_review_required',dep['task_id'],{'document_id':data['document_id'],
                    'pinned_version':dep['pinned_version'],'new_version':data['new_version']},actor='kernel',cause=data['event_id'],prior=dep['pinned_version'])
                impacted = c.execute(text("SELECT work_id,status FROM work_items WHERE task_id=:task AND status IN ('COMMITTED','READY','RUNNING') FOR UPDATE"), {'task':dep['task_id']}).mappings().all()
                for work in impacted:
                    if work['status'] == 'COMMITTED':
                        self._transition(c,work['work_id'],'STALE',data['event_id'])
                        self.stale_descendants(c,work['work_id'],data['event_id'])
                    elif work['status'] == 'READY':
                        self._transition(c,work['work_id'],'BLOCKED',data['event_id'])
                    else:
                        active = one(c,"SELECT run_id FROM runs WHERE work_id=:id AND status='RUNNING'", {'id':work['work_id']})
                        if active:
                            c.execute(text("UPDATE runs SET status='FAILED',error='source became stale',finished_at=now() WHERE run_id=:id"), {'id':active['run_id']})
                            c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id':work['work_id']})
                            self._event(c,'run.failed',active['run_id'],{'reason':'source became stale'},cause=data['event_id'])
                        self._transition(c,work['work_id'],'REWORK_REQUIRED',data['event_id'])
            return root, True

    def prepare_policy(self, run_id):
        with self.engine.begin() as c:
            self._lock(c)
            run = one(c, """SELECT r.* FROM runs r JOIN work_items w ON w.work_id=r.work_id
                JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING'
                AND q.status='CLAIMED' AND q.lease_until>now() AND w.task_id='TASK-002' AND w.task_version=2""", {'id':run_id})
            if not run:
                raise Conflict('run is not an eligible policy task')
            manifest = build_policy_manifest(c,run_id,run['work_id'])
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            c.execute(text("UPDATE runs SET agent_version='policy-deterministic-v1',model_id='MOCK',prompt_version='none' WHERE run_id=:id"), {'id':run_id})
            self._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),'event_id':manifest['event_id']})
            return manifest

    def prepare_policy_verification(self, run_id):
        """Snapshot the accepted root artifact, its immutable context and its evidence."""
        with self.engine.begin() as c:
            self._lock(c)
            row = one(c, '''SELECT r.work_id,w.parent_work_id,w.input_snapshot,p.accepted_artifact_id,
                a.content,a.sha256,ctx.manifest,ctx.sha256 AS context_hash,e.result,e.verifier_version
                FROM runs r JOIN work_items w ON w.work_id=r.work_id
                JOIN work_queue q ON q.run_id=r.run_id JOIN work_items p ON p.work_id=w.parent_work_id
                JOIN work_dependencies d ON d.predecessor_id=p.work_id AND d.successor_id=w.work_id
                JOIN artifacts a ON a.artifact_id=p.accepted_artifact_id AND d.pinned_artifact_version=a.artifact_id
                JOIN runs parent_run ON parent_run.run_id=a.run_id AND parent_run.status='SUCCEEDED'
                JOIN run_contexts ctx ON ctx.run_id=parent_run.run_id
                JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='policy_deterministic'
                WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until>now()
                AND p.status='COMMITTED' ''', {'id':run_id})
            if not row or row['input_snapshot'].get('step_no') != 1:
                raise Conflict('policy verification is not eligible')
            manifest = {'run_id':str(run_id),'work_id':str(row['work_id']),'task_id':'TASK-002',
                        'task_version':2,'step_no':1,'parent_work_id':str(row['parent_work_id']),
                        'parent_artifact_id':str(row['accepted_artifact_id']), 'parent_artifact':row['content'],
                        'parent_artifact_sha256':row['sha256'],'parent_manifest':row['manifest'],
                        'parent_context_sha256':row['context_hash'],'parent_evidence':row['result'],
                        'parent_verifier':row['verifier_version'],'data_mode':'DEMO DATA',
                        'model_id':'MOCK','graph_version':'policy-followup-verification-v1'}
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            c.execute(text("UPDATE runs SET agent_version='policy-followup-verification-v1',model_id='MOCK',prompt_version='none' WHERE run_id=:id"), {'id':run_id})
            self._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                        'parent_artifact_id':manifest['parent_artifact_id']})
            return manifest

    def prepare_policy_index(self, run_id):
        """Snapshot a verified predecessor and the immutable demo policy version to index."""
        _,index = demo_index_target()
        with self.engine.begin() as c:
            self._lock(c)
            row = one(c, '''SELECT r.work_id,w.input_snapshot,w.parent_work_id,p.work_id AS predecessor_id,
                p.accepted_artifact_id AS predecessor_artifact_id,a.content AS predecessor_artifact,
                a.sha256 AS predecessor_sha,e.result AS predecessor_evidence,
                root.accepted_artifact_id AS root_artifact_id,rootctx.manifest AS root_manifest,rootctx.sha256 AS root_context_hash,
                evt.document_id,evt.new_version,evt.new_sha256,doc.title,doc.clauses,doc.sha256
                FROM runs r JOIN work_items w ON w.work_id=r.work_id
                JOIN work_queue q ON q.run_id=r.run_id
                JOIN work_dependencies d ON d.successor_id=w.work_id
                JOIN work_items p ON p.work_id=d.predecessor_id AND p.status='COMMITTED'
                    AND d.pinned_artifact_version=p.accepted_artifact_id
                JOIN artifacts a ON a.artifact_id=p.accepted_artifact_id
                JOIN runs pr ON pr.run_id=a.run_id AND pr.status='SUCCEEDED'
                JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='policy_followup_recheck'
                JOIN work_items root ON root.work_id=w.parent_work_id AND root.status='COMMITTED'
                JOIN work_dependencies rd ON rd.predecessor_id=root.work_id AND rd.successor_id=p.work_id
                    AND rd.pinned_artifact_version=root.accepted_artifact_id
                JOIN artifacts ra ON ra.artifact_id=root.accepted_artifact_id
                JOIN run_contexts rootctx ON rootctx.run_id=ra.run_id
                JOIN policy_change_events evt ON evt.root_work_id=root.work_id
                JOIN policy_documents doc ON doc.document_id=evt.document_id AND doc.version=evt.new_version
                WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until>now() ''', {'id':run_id})
            if not row or row['input_snapshot'].get('step_no') != 2:
                raise Conflict('policy indexing is not eligible')
            source = row['root_manifest']
            if (row['document_id'] != 'POLICY-VOC' or digest(row['clauses']) != row['sha256']
                    or row['sha256'] != row['new_sha256'] or digest(source) != row['root_context_hash']
                    or row['predecessor_evidence'].get('passed') is not True
                    or digest(row['predecessor_artifact']) != row['predecessor_sha']
                    or row['predecessor_artifact'].get('parent_artifact_id') != str(row['root_artifact_id'])
                    or source['retrieval'].get('mode') != 'OPENSEARCH' or source['retrieval'].get('index') != index):
                raise Conflict('policy source or predecessor does not match')
            document = {'document_id':row['document_id'],'version':row['new_version'],'title':row['title'],
                        'body':canonical(row['clauses']),'sha256':row['sha256']}
            manifest = {'run_id':str(run_id),'work_id':str(row['work_id']),'task_id':'TASK-002','task_version':2,
                        'step_no':2,'root_work_id':str(row['parent_work_id']),'root_artifact_id':str(row['root_artifact_id']),
                        'root_context_sha256':row['root_context_hash'],'predecessor_work_id':str(row['predecessor_id']),
                        'predecessor_artifact_id':str(row['predecessor_artifact_id']),
                        'predecessor_artifact_sha256':row['predecessor_sha'],'document':document,
                        'previous_indexed_document':source['indexed_previous_document'],'index':index,
                        'data_mode':'DEMO DATA','model_id':'MOCK','graph_version':'policy-index-v1'}
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            c.execute(text("UPDATE runs SET agent_version='policy-index-v1',model_id='MOCK',prompt_version='none' WHERE run_id=:id"), {'id':run_id})
            self._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                        'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':row['document_id'],
                        'document_version':row['new_version'],'index':index})
            return manifest

    def prepare_policy_regression(self, run_id):
        _,index = demo_index_target()
        with self.engine.begin() as c:
            self._lock(c)
            row = one(c, '''SELECT r.work_id,w.input_snapshot,w.parent_work_id,p.work_id AS predecessor_id,
                p.accepted_artifact_id,a.content,a.sha256,e.result AS evidence
                FROM runs r JOIN work_items w ON w.work_id=r.work_id JOIN work_queue q ON q.run_id=r.run_id
                JOIN work_dependencies d ON d.successor_id=w.work_id
                JOIN work_items p ON p.work_id=d.predecessor_id AND p.status='COMMITTED' AND p.accepted_artifact_id=d.pinned_artifact_version
                JOIN artifacts a ON a.artifact_id=p.accepted_artifact_id
                JOIN runs pr ON pr.run_id=a.run_id AND pr.status='SUCCEEDED'
                JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='policy_index_recheck'
                WHERE r.run_id=:id AND r.status='RUNNING' AND q.status='CLAIMED' AND q.lease_until>now()''', {'id':run_id})
            if (not row or row['input_snapshot'].get('step_no')!=3 or row['evidence'].get('passed') is not True
                    or digest(row['content'])!=row['sha256'] or not self._policy_regression_chain_current(c,row['work_id'])):
                raise Conflict('policy regression is not eligible')
            receipt = row['content']
            source = one(c,'''SELECT title,clauses,sha256 FROM policy_documents WHERE document_id=:id AND version=:version''',
                         {'id':receipt['document_id'],'version':receipt['document_version']})
            if not source or receipt['index']!=index or digest(source['clauses'])!=source['sha256'] or source['sha256']!=receipt['document_sha256']:
                raise Conflict('indexed policy source changed')
            doc = {'id':f"{receipt['document_id']}@{receipt['document_version']}",'title':source['title'],
                   'body':canonical(source['clauses']),'sha256':source['sha256']}
            documents,retrieval = retrieve_documents([doc['id']])
            if documents!=[doc]:
                raise Conflict('indexed policy is unavailable or changed')
            manifest = {'run_id':str(run_id),'work_id':str(row['work_id']),'task_id':'TASK-002','task_version':2,
                        'step_no':3,'root_work_id':str(row['parent_work_id']),
                        'predecessor_work_id':str(row['predecessor_id']),'predecessor_artifact_id':str(row['accepted_artifact_id']),
                        'predecessor_artifact_sha256':row['sha256'],'document':doc,'retrieval':retrieval,
                        'evaluation_inputs':load_regression_inputs(c),'data_mode':'DEMO DATA',
                        'model_id':'MOCK','graph_version':'policy-regression-v1'}
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            c.execute(text("UPDATE runs SET agent_version='policy-regression-v1',model_id='MOCK',prompt_version='none' WHERE run_id=:id"), {'id':run_id})
            self._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                        'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':doc['id'],
                        'suite_hashes':[s['sha256'] for s in manifest['evaluation_inputs']['suites']],
                        'active_method':manifest['evaluation_inputs']['active']['method_id']})
            return manifest

    def _policy_review_context(self, c, root_id):
        event = one(c,'SELECT * FROM policy_change_events WHERE root_work_id=:id',{'id':root_id})
        if not event:
            raise NotFound('policy work not found')
        followups = c.execute(text('''SELECT f.step_no,w.work_id,w.status,w.version FROM policy_followups f
            JOIN work_items w ON w.work_id=f.work_id WHERE f.root_work_id=:id ORDER BY f.step_no'''),{'id':root_id}).mappings().all()
        if len(followups)!=4 or followups[3]['status']!='WAITING_APPROVAL':
            raise Conflict('review is not waiting for a decision')
        if not self._policy_regression_chain_current(c,followups[2]['work_id']):
            raise Conflict('policy prerequisites are not current')
        stages = [('analysis',root_id,'policy_deterministic'),('recheck',followups[0]['work_id'],'policy_followup_recheck'),
                  ('index',followups[1]['work_id'],'policy_index_recheck'),('regression',followups[2]['work_id'],'policy_regression_check')]
        snapshots,regression,analysis = [],None,None
        for name,work_id,kind in stages:
            row = one(c,'''SELECT w.status,a.artifact_id,a.sha256,a.content,r.status AS run_status,
                ctx.manifest,ctx.sha256 AS context_sha256,e.evidence_id,e.result,e.verifier_version
                FROM work_items w JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id
                JOIN runs r ON r.run_id=a.run_id JOIN run_contexts ctx ON ctx.run_id=r.run_id
                JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type=:kind
                WHERE w.work_id=:id''',{'id':work_id,'kind':kind})
            if (not row or row['status']!='COMMITTED' or row['run_status']!='SUCCEEDED'
                    or row['result'].get('passed') is not True or digest(row['content'])!=row['sha256']
                    or digest(row['manifest'])!=row['context_sha256']):
                raise Conflict('review prerequisites are not verified')
            snapshots.append({'stage':name,'work_id':str(work_id),'artifact_id':str(row['artifact_id']),
                'artifact_sha256':row['sha256'],'context_sha256':row['context_sha256'],
                'evidence_id':str(row['evidence_id']),'evidence_sha256':digest(row['result']),
                'verifier_version':row['verifier_version']})
            if name=='regression':
                regression=row
            elif name=='analysis':
                analysis=row
        manifest = regression['manifest']
        try:
            inputs = load_regression_inputs(c)
        except (ValueError,NoResultFound) as exc:
            raise Conflict('review evaluation inputs are unavailable or changed') from exc
        if inputs!=manifest['evaluation_inputs']:
            raise Conflict('VOC method or evaluation inputs changed')
        doc = manifest['document']
        source = one(c,'SELECT title,clauses,sha256 FROM policy_documents WHERE document_id=:id AND version=:v',
                     {'id':event['document_id'],'v':event['new_version']})
        if (not source or doc['id']!=f"{event['document_id']}@{event['new_version']}"
                or source['title']!=doc['title'] or canonical(source['clauses'])!=doc['body']
                or digest(source['clauses'])!=source['sha256'] or source['sha256']!=doc['sha256']
                or source['sha256']!=event['new_sha256']):
            raise Conflict('review policy source changed')
        old_source = one(c,'SELECT title,clauses,sha256 FROM policy_documents WHERE document_id=:id AND version=:v',
                         {'id':event['document_id'],'v':event['old_version']})
        if not old_source or digest(old_source['clauses'])!=old_source['sha256'] or old_source['sha256']!=event['old_sha256']:
            raise Conflict('previous review policy source changed')
        old_doc = {'id':f"{event['document_id']}@{event['old_version']}",'title':old_source['title'],
                   'body':canonical(old_source['clauses']),'sha256':old_source['sha256']}
        if analysis['manifest']['indexed_previous_document']!=[old_doc]:
            raise Conflict('previous review policy snapshot changed')
        _,index = demo_index_target()
        documents,retrieval = retrieve_documents([old_doc['id'],doc['id']])
        if (documents!=sorted([old_doc,doc],key=lambda item:item['id'])
                or retrieval['index']!=manifest['retrieval']['index'] or index!=retrieval['index']):
            raise Conflict('review policy index changed')
        snapshot = {'root_work_id':str(root_id),'review_work_id':str(followups[3]['work_id']),
                    'review_work_version':followups[3]['version'],'event_id':event['event_id'],
                    'document_id':doc['id'],'document_sha256':doc['sha256'],
                    'previous_document_id':old_doc['id'],'previous_document_sha256':old_doc['sha256'],'search_index':index,
                    'stages':snapshots,'method_id':inputs['active']['method_id'],
                    'method_sha256':inputs['active']['sha256'],'method_state_version':inputs['state']['version'],
                    'suites':[{'suite_version':s['suite_version'],'sha256':s['sha256']} for s in inputs['suites']],
                    'scope':'HUMAN_REVIEW_RECORD_NOT_POLICY_APPLICATION'}
        return {'context':snapshot,'context_sha256':digest(snapshot)}

    def policy_review_context(self, root_id):
        with self.engine.connect() as c:
            return self._policy_review_context(c,root_id)

    def decide_policy_review(self, root_id, request):
        data = request.model_dump()
        note = data['note'].strip()
        if not note:
            raise Conflict('review note must not be blank')
        with self.engine.begin() as c:
            self._lock(c)
            key = 'policy.review_decided:'+data['idempotency_key']
            previous = one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
            if previous:
                payload=previous['payload']
                if (payload.get('root_work_id')!=root_id or payload.get('context_sha256')!=data['context_sha256']
                        or payload.get('decision')!=data['decision'] or payload.get('note')!=note):
                    raise Conflict('review idempotency key already used')
                return payload['artifact_id'],False
            prepared = self._policy_review_context(c,root_id)
            if prepared['context_sha256']!=data['context_sha256']:
                raise Conflict('review context changed; inspect the new evidence')
            snapshot = prepared['context']
            review = self._work(c,snapshot['review_work_id'])
            run_id,artifact_id = uid(),uid()
            attempt = one(c,'SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM runs WHERE work_id=:id',{'id':review['work_id']})['n']
            predecessor = snapshot['stages'][-1]
            c.execute(text('UPDATE work_dependencies SET pinned_artifact_version=:artifact WHERE predecessor_id=:pred AND successor_id=:review'),
                      {'artifact':predecessor['artifact_id'],'pred':predecessor['work_id'],'review':review['work_id']})
            self._event(c,'dependency.pinned',review['work_id'],{'predecessor_id':predecessor['work_id'],
                        'artifact_id':predecessor['artifact_id']},actor='user',cause=key)
            self._transition(c,review['work_id'],'READY',key)
            c.execute(text('''INSERT INTO runs(run_id,work_id,attempt_no,status,agent_version,model_id,prompt_version)
                VALUES (:run,:work,:n,'RUNNING','human-review-v1','HUMAN','none')'''),
                {'run':run_id,'work':review['work_id'],'n':attempt})
            self._transition(c,review['work_id'],'RUNNING',run_id)
            self._event(c,'run.started',run_id,{'work_id':str(review['work_id']),'attempt_no':attempt,'actor_type':'HUMAN'},actor='user',cause=key)
            manifest = {'run_id':run_id,'work_id':str(review['work_id']),'task_id':'TASK-002','task_version':2,
                        'data_mode':'DEMO DATA','review_context':snapshot,'review_context_sha256':prepared['context_sha256'],
                        'model_id':'HUMAN','graph_version':'human-review-v1'}
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            report = PolicyReviewReport(task_id='TASK-002',data_mode='DEMO DATA',step_no=4,
                context_sha256=prepared['context_sha256'],decision=data['decision'],note=note,policy_applied=False).model_dump()
            artifact_hash = store_json(c,artifact_id,run_id,report,'policy_human_review')
            self._transition(c,review['work_id'],'SUBMITTED',run_id)
            self._transition(c,review['work_id'],'VERIFYING',run_id)
            checks = {'explicit_user_decision':report['decision'] in ('REVIEWED','CHANGES_REQUESTED') and bool(report['note'].strip()),
                      'context_current':digest(snapshot)==prepared['context_sha256']==data['context_sha256'],
                      'predecessor_evidence_passed':[s['stage'] for s in snapshot['stages']]==['analysis','recheck','index','regression']
                          and all(s['evidence_id'] and s['evidence_sha256'] for s in snapshot['stages']),
                      'policy_not_applied':report['policy_applied'] is False}
            result = {'passed':all(checks.values()),'checks':checks,'scope':'RECORD_INTEGRITY_ONLY_NOT_SEMANTIC_OR_POLICY_APPROVAL'}
            if not result['passed']:
                raise Conflict('human review record failed integrity checks')
            refs = {'artifact_sha256':artifact_hash,'review_context_sha256':prepared['context_sha256'],
                    'context_sha256':digest(manifest),'stages':snapshot['stages'],'document_id':snapshot['document_id']}
            c.execute(text('''INSERT INTO evidence(evidence_id,artifact_id,verification_type,input_refs,result,verifier_version)
                VALUES (:id,:artifact,'human_review_record',CAST(:refs AS jsonb),CAST(:result AS jsonb),'human-review-record-verifier-v1')'''),
                {'id':uid(),'artifact':artifact_id,'refs':canonical(refs),'result':canonical(result)})
            c.execute(text('''INSERT INTO evaluations(evaluation_id,run_id,artifact_id,eval_suite_version,metric_results,verdict)
                VALUES (:id,:run,:artifact,'human-review-record-suite-v1',CAST(:result AS jsonb),'PASS')'''),
                {'id':uid(),'run':run_id,'artifact':artifact_id,'result':canonical(result)})
            self._transition(c,review['work_id'],'ACCEPTED',run_id)
            c.execute(text('UPDATE work_items SET accepted_artifact_id=:artifact WHERE work_id=:id'),{'artifact':artifact_id,'id':review['work_id']})
            self._transition(c,review['work_id'],'COMMITTED',run_id)
            c.execute(text("UPDATE runs SET status='SUCCEEDED',finished_at=now() WHERE run_id=:id"),{'id':run_id})
            payload={'root_work_id':root_id,'context_sha256':prepared['context_sha256'],'decision':data['decision'],
                     'note':note,'run_id':run_id,'artifact_id':artifact_id}
            self._event(c,'policy.review_decided',review['work_id'],payload,actor='user',cause=snapshot['event_id'],
                        prior=review['version'],key=key)
            self._event(c,'run.verified',run_id,{'artifact_id':artifact_id,**result},actor='kernel',cause=key)
            return artifact_id,True

    def reopen_policy_review(self, root_id, key):
        with self.engine.begin() as c:
            self._lock(c)
            event_key='policy.review_reopened:'+key
            previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':event_key})
            if previous:
                if previous['payload'].get('root_work_id')!=root_id:
                    raise Conflict('review reopen key already used')
                return False
            review=one(c,'''SELECT w.* FROM policy_followups f JOIN work_items w ON w.work_id=f.work_id
                WHERE f.root_work_id=:root AND f.step_no=4 FOR UPDATE OF w''',{'root':root_id})
            if not review or review['status']!='COMMITTED':
                raise Conflict('only a recorded review can be reopened')
            self._transition(c,review['work_id'],'STALE',key)
            self._transition(c,review['work_id'],'WAITING_APPROVAL',key)
            self._event(c,'policy.review_reopened',review['work_id'],{'root_work_id':root_id,
                        'previous_artifact_id':str(review['accepted_artifact_id'])},actor='user',cause=key,
                        prior=review['version'],key=event_key)
            return True

    def review_policy_note(self, work_id, request):
        """Record an explicit human observation; it does not approve or update policy."""
        data = request.model_dump()
        note = data['note'].strip()
        if not note:
            raise Conflict('review note must not be blank')
        with self.engine.begin() as c:
            self._lock(c)
            key = 'policy.review_note:'+data['idempotency_key']
            previous = one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
            payload = {'artifact_id':data['artifact_id'],'note':note,'decision':'NOTE_ONLY'}
            if previous:
                if previous['subject_id'] != work_id or previous['payload'] != payload:
                    raise Conflict('review note idempotency key already used')
                return False
            work = self._work(c,work_id)
            event = one(c,'SELECT event_id FROM policy_change_events WHERE root_work_id=:id',{'id':work_id})
            if not event or work['status'] != 'COMMITTED' or str(work['accepted_artifact_id']) != data['artifact_id']:
                raise Conflict('review note requires the current verified policy analysis')
            self._event(c,'policy.review_note',work_id,payload,actor='user',cause=event['event_id'],
                        prior=work['version'],key=key)
            return True

    def _previous_voc_report(self, c, artifact_id=None):
        from app.workloads.voc_verifier import previous_voc_report
        return previous_voc_report(c, artifact_id)

    def prepare_voc(self, run_id):
        with self.engine.begin() as c:
            self._lock(c)
            run = one(c, """SELECT r.*,w.task_version FROM runs r JOIN work_items w ON w.work_id=r.work_id
                JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING'
                AND q.status='CLAIMED' AND q.lease_until>now() AND w.task_id='TASK-003' AND w.task_version IN (2,3,4)""", {'id':run_id})
            if not run:
                raise Conflict('run is not an eligible VOC task')
            method = one(c, '''SELECT m.* FROM voc_method_state s JOIN voc_method_versions m ON m.method_id=s.active_method_id
                WHERE s.task_id='TASK-003' ''')
            with source_engine(self.engine).connect() as source_conn:
                records = [dict(row) for row in source_conn.execute(text(VOC_RECORD_QUERY),{'version':'voc-demo-v1'}).mappings()]
            source = one(c, "SELECT * FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")
            if not method or not records:
                raise Conflict('VOC inputs or method not configured')
            manifest = {'run_id':str(run_id),'work_id':str(run['work_id']),'task_id':'TASK-003','task_version':run['task_version'],
                        'data_mode':'DEMO DATA','source_version':'voc-demo-v1','records':records,'records_sha256':digest(records),'sql_source_mode':source_mode(),
                        'method':dict(method),'source_review_status':source['status'] if source else 'NOT_REGISTERED',
                        'source_dependency':dict(source) if source else None,'model_id':'MOCK','graph_version':'voc-rules-v1'}
            if run['task_version']>=3:
                previous = self._previous_voc_report(c)
                if previous and digest(previous['content'])!=previous['sha256']:
                    raise Conflict('previous VOC artifact hash mismatch')
                manifest.update(previous_report=previous,history_as_of=c.execute(text('SELECT clock_timestamp()')).scalar_one().isoformat(),
                                graph_version='voc-history-v2')
            if run['task_version']>=4:
                with source_engine(self.engine).connect() as source_conn:
                    docs = source_conn.execute(text(VOC_DOCUMENT_QUERY),VOC_DOCUMENT_PARAMETERS).mappings().all()
                authoritative = [{'id':f"{d['document_id']}@{d['version']}",'title':d['title'],'body':d['body'],'sha256':d['sha256']}
                                 for d in docs if f"{d['document_id']}@{d['version']}" in VOC_DOCUMENTS.values()]
                mode,_,_ = search_configuration()
                if mode=='OPENSEARCH':
                    documents,retrieval = retrieve_documents(list(VOC_DOCUMENTS.values()))
                else:
                    documents = list(authoritative)
                    retrieval = {'mode':'FIXTURE_DB','query':'PostgreSQL source_documents by document/version',
                                 'document_ids':list(VOC_DOCUMENTS.values())}
                manifest.update(documents=documents,authoritative_documents=authoritative,retrieval=retrieval,
                                retrieval_result_sha256=digest(documents),graph_version='voc-sources-v3',
                                record_query=VOC_RECORD_QUERY,record_parameters={'version':'voc-demo-v1'},
                                document_query=VOC_DOCUMENT_QUERY,document_parameters=VOC_DOCUMENT_PARAMETERS)
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
            c.execute(text("UPDATE runs SET agent_version=:method,model_id='MOCK',prompt_version='none' WHERE run_id=:id"),
                      {'id':run_id,'method':method['method_id']})
            self._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),'method_version':method['method_id']})
            return manifest

    def prepare_market(self, run_id):
        with self.engine.connect() as c:
            run = one(c, """SELECT r.work_id,r.attempt_no,w.task_id,w.task_version,w.input_snapshot FROM runs r
                JOIN work_items w ON w.work_id=r.work_id WHERE r.run_id=:id AND r.status='RUNNING'""", {'id':run_id})
            if not run or run['task_id'] != 'TASK-001' or run['task_version'] != 2:
                raise Conflict('run is not an eligible market task')
            snapshot = dict(run['input_snapshot'])
            # Fault injection affects only the first attempt; retry reconstructs full sources.
            if run['attempt_no'] > 1:
                snapshot['omit_document'] = False
            manifest = build_manifest(self.engine, run_id, run['work_id'], run['task_version'], snapshot)
        with self.engine.begin() as c:
            self._lock(c)
            active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
            if not active:
                raise Conflict('run lease expired')
            c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                      {'id':run_id, 'manifest':canonical(manifest), 'sha':digest(manifest)})
            self._event(c, 'run.context_created', run_id, {'manifest_sha256':digest(manifest), 'dataset_version':manifest['dataset_version']})
        return manifest

    def record_market_model(self, run_id, metadata):
        with self.engine.begin() as c:
            self._lock(c)
            active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
            if not active:
                raise Conflict('run lease expired')
            c.execute(text("UPDATE runs SET agent_version=:agent,model_id=:model,prompt_version=:prompt WHERE run_id=:id AND status='RUNNING'"),
                      {'agent':'market-'+metadata['model_mode'], 'model':metadata['model_id'], 'prompt':PROMPT_VERSION, 'id':run_id})
            self._event(c, 'run.model_completed', run_id, metadata)

    def record_market_model_started(self, run_id, mode, model):
        if mode not in ('MOCK','LIVE_LLM'):
            raise ValueError('unsupported model mode')
        with self.engine.begin() as c:
            self._lock(c)
            active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
            if not active:
                raise Conflict('run lease expired')
            c.execute(text("UPDATE runs SET agent_version=:agent,model_id=:model,prompt_version=:prompt WHERE run_id=:id AND status='RUNNING'"),
                      {'agent':'market-'+mode, 'model':model, 'prompt':PROMPT_VERSION, 'id':run_id})
            self._event(c, 'run.model_started', run_id, {'model_mode':mode,'model_id':model,'prompt_version':PROMPT_VERSION})

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
            if one(c,'SELECT 1 FROM text_experiment_work WHERE work_id=:id',{'id':work_id}):
                raise Conflict('evaluation-only work cannot be accepted as a production result')
            if decision == 'ACCEPT' and not one(c,'SELECT 1 FROM task_definitions WHERE task_id=:task AND version=:version AND active',
                                               {'task':work['task_id'],'version':work['task_version']}):
                raise Conflict('work was executed under an inactive task version')
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
            else:
                self._transition(c,work_id,'REWORK_REQUIRED',str(artifact_id))
            self._event(c,'work.reviewed',work_id,payload,actor='user',cause=str(artifact_id),key=key)
            return True
