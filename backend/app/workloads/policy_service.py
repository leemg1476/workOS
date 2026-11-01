"""Policy demo event, context assembly and human review services."""
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from app.domain.contracts import PolicyReviewReport
from app.integrations.artifact_store import store_json
from app.integrations.search import retrieve_documents, demo_index_target
from app.evaluation.policy_regression import load_inputs as load_regression_inputs
from app.work.market import digest
from app.work.policy import STEPS, build_policy_manifest
from app.work.service import Conflict, NotFound, canonical, one, uid
from app.workloads.policy_verifiers import policy_regression_chain_current

def document_updated(kernel, request):
    """Register a source change, affected tasks and follow-up DAG atomically."""
    data = request.model_dump()
    with kernel.engine.begin() as c:
        kernel._lock(c)
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
            kernel._event(c,'work.created',work_id,{'job_id':job['job_id'],'input_snapshot':snapshot},actor='webhook',cause=data['event_id'])
        create(root,None,{'source_event':data['event_id'],'document_id':data['document_id'],
                          'old_version':data['old_version'],'new_version':data['new_version']})
        affected = [d['task_id'] for d in dependencies]
        c.execute(text('''INSERT INTO policy_change_events(event_id,document_id,old_version,new_version,old_sha256,new_sha256,affected_tasks,root_work_id)
            VALUES (:event_id,:document_id,:old_version,:new_version,:old_sha256,:new_sha256,CAST(:affected AS jsonb),:root)'''),
            {**data,'affected':canonical(affected),'root':root})
        kernel._event(c,'document.updated',root,data,actor='webhook',cause=data['event_id'],key='document.updated:'+data['event_id'])
        previous = root
        for index,title in enumerate(STEPS,1):
            child = uid()
            create(child,root,{'source_event':data['event_id'],'step_no':index,'title':title,'requires_human_or_future_worker':True})
            c.execute(text('INSERT INTO policy_followups(root_work_id,work_id,step_no,title) VALUES (:root,:child,:n,:title)'),
                      {'root':root,'child':child,'n':index,'title':title})
            c.execute(text("INSERT INTO work_dependencies(predecessor_id,successor_id,dependency_kind) VALUES (:p,:s,'policy_followup')"),
                      {'p':previous,'s':child})
            kernel._event(c,'dependency.created',child,{'predecessor_id':previous,'kind':'policy_followup'},cause=data['event_id'])
            kernel._transition(c,child,'WAITING_APPROVAL' if index == len(STEPS) else 'BLOCKED',data['event_id'])
            previous = child
        kernel._transition(c,root,'READY',data['event_id'])
        c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"), {'id':root})
        for dep in dependencies:
            c.execute(text("UPDATE task_source_dependencies SET status='REVIEW_REQUIRED' WHERE task_id=:task AND document_id=:doc"),
                      {'task':dep['task_id'],'doc':data['document_id']})
            kernel._event(c,'task.source_review_required',dep['task_id'],{'document_id':data['document_id'],
                'pinned_version':dep['pinned_version'],'new_version':data['new_version']},actor='kernel',cause=data['event_id'],prior=dep['pinned_version'])
            impacted = c.execute(text("SELECT work_id,status FROM work_items WHERE task_id=:task AND status IN ('COMMITTED','READY','RUNNING') FOR UPDATE"), {'task':dep['task_id']}).mappings().all()
            for work in impacted:
                if work['status'] == 'COMMITTED':
                    kernel._transition(c,work['work_id'],'STALE',data['event_id'])
                    kernel.stale_descendants(c,work['work_id'],data['event_id'])
                elif work['status'] == 'READY':
                    kernel._transition(c,work['work_id'],'BLOCKED',data['event_id'])
                else:
                    active = one(c,"SELECT run_id FROM runs WHERE work_id=:id AND status='RUNNING'", {'id':work['work_id']})
                    if active:
                        c.execute(text("UPDATE runs SET status='FAILED',error='source became stale',finished_at=now() WHERE run_id=:id"), {'id':active['run_id']})
                        c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"), {'id':work['work_id']})
                        kernel._event(c,'run.failed',active['run_id'],{'reason':'source became stale'},cause=data['event_id'])
                    kernel._transition(c,work['work_id'],'REWORK_REQUIRED',data['event_id'])
        return root, True


def prepare_policy(kernel, run_id):
    with kernel.engine.begin() as c:
        kernel._lock(c)
        run = one(c, """SELECT r.* FROM runs r JOIN work_items w ON w.work_id=r.work_id
            JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING'
            AND q.status='CLAIMED' AND q.lease_until>now() AND w.task_id='TASK-002' AND w.task_version=2""", {'id':run_id})
        if not run:
            raise Conflict('run is not an eligible policy task')
        manifest = build_policy_manifest(c,run_id,run['work_id'])
        c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                  {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
        c.execute(text("UPDATE runs SET agent_version='policy-deterministic-v1',model_id='MOCK',prompt_version='none' WHERE run_id=:id"), {'id':run_id})
        kernel._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),'event_id':manifest['event_id']})
        return manifest


def prepare_policy_verification(kernel, run_id):
    """Snapshot the accepted root artifact, its immutable context and its evidence."""
    with kernel.engine.begin() as c:
        kernel._lock(c)
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
        kernel._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                    'parent_artifact_id':manifest['parent_artifact_id']})
        return manifest


def prepare_policy_index(kernel, run_id):
    """Snapshot a verified predecessor and the immutable demo policy version to index."""
    _,index = demo_index_target()
    with kernel.engine.begin() as c:
        kernel._lock(c)
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
        kernel._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                    'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':row['document_id'],
                    'document_version':row['new_version'],'index':index})
        return manifest


def prepare_policy_regression(kernel, run_id):
    _,index = demo_index_target()
    with kernel.engine.begin() as c:
        kernel._lock(c)
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
                or digest(row['content'])!=row['sha256'] or not policy_regression_chain_current(c,row['work_id'])):
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
        kernel._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),
                    'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':doc['id'],
                    'suite_hashes':[s['sha256'] for s in manifest['evaluation_inputs']['suites']],
                    'active_method':manifest['evaluation_inputs']['active']['method_id']})
        return manifest


def _policy_review_context(kernel, c, root_id):
    event = one(c,'SELECT * FROM policy_change_events WHERE root_work_id=:id',{'id':root_id})
    if not event:
        raise NotFound('policy work not found')
    followups = c.execute(text('''SELECT f.step_no,w.work_id,w.status,w.version FROM policy_followups f
        JOIN work_items w ON w.work_id=f.work_id WHERE f.root_work_id=:id ORDER BY f.step_no'''),{'id':root_id}).mappings().all()
    if len(followups)!=4 or followups[3]['status']!='WAITING_APPROVAL':
        raise Conflict('review is not waiting for a decision')
    if not policy_regression_chain_current(c,followups[2]['work_id']):
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


def policy_review_context(kernel, root_id):
    with kernel.engine.connect() as c:
        return _policy_review_context(kernel,c,root_id)


def decide_policy_review(kernel, root_id, request):
    data = request.model_dump()
    note = data['note'].strip()
    if not note:
        raise Conflict('review note must not be blank')
    with kernel.engine.begin() as c:
        kernel._lock(c)
        key = 'policy.review_decided:'+data['idempotency_key']
        previous = one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            payload=previous['payload']
            if (payload.get('root_work_id')!=root_id or payload.get('context_sha256')!=data['context_sha256']
                    or payload.get('decision')!=data['decision'] or payload.get('note')!=note):
                raise Conflict('review idempotency key already used')
            return payload['artifact_id'],False
        prepared = _policy_review_context(kernel,c,root_id)
        if prepared['context_sha256']!=data['context_sha256']:
            raise Conflict('review context changed; inspect the new evidence')
        snapshot = prepared['context']
        review = kernel._work(c,snapshot['review_work_id'])
        run_id,artifact_id = uid(),uid()
        attempt = one(c,'SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM runs WHERE work_id=:id',{'id':review['work_id']})['n']
        predecessor = snapshot['stages'][-1]
        c.execute(text('UPDATE work_dependencies SET pinned_artifact_version=:artifact WHERE predecessor_id=:pred AND successor_id=:review'),
                  {'artifact':predecessor['artifact_id'],'pred':predecessor['work_id'],'review':review['work_id']})
        kernel._event(c,'dependency.pinned',review['work_id'],{'predecessor_id':predecessor['work_id'],
                    'artifact_id':predecessor['artifact_id']},actor='user',cause=key)
        kernel._transition(c,review['work_id'],'READY',key)
        c.execute(text('''INSERT INTO runs(run_id,work_id,attempt_no,status,agent_version,model_id,prompt_version)
            VALUES (:run,:work,:n,'RUNNING','human-review-v1','HUMAN','none')'''),
            {'run':run_id,'work':review['work_id'],'n':attempt})
        kernel._transition(c,review['work_id'],'RUNNING',run_id)
        kernel._event(c,'run.started',run_id,{'work_id':str(review['work_id']),'attempt_no':attempt,'actor_type':'HUMAN'},actor='user',cause=key)
        manifest = {'run_id':run_id,'work_id':str(review['work_id']),'task_id':'TASK-002','task_version':2,
                    'data_mode':'DEMO DATA','review_context':snapshot,'review_context_sha256':prepared['context_sha256'],
                    'model_id':'HUMAN','graph_version':'human-review-v1'}
        c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                  {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
        report = PolicyReviewReport(task_id='TASK-002',data_mode='DEMO DATA',step_no=4,
            context_sha256=prepared['context_sha256'],decision=data['decision'],note=note,policy_applied=False).model_dump()
        artifact_hash = store_json(c,artifact_id,run_id,report,'policy_human_review')
        kernel._transition(c,review['work_id'],'SUBMITTED',run_id)
        kernel._transition(c,review['work_id'],'VERIFYING',run_id)
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
        kernel._transition(c,review['work_id'],'ACCEPTED',run_id)
        c.execute(text('UPDATE work_items SET accepted_artifact_id=:artifact WHERE work_id=:id'),{'artifact':artifact_id,'id':review['work_id']})
        kernel._transition(c,review['work_id'],'COMMITTED',run_id)
        c.execute(text("UPDATE runs SET status='SUCCEEDED',finished_at=now() WHERE run_id=:id"),{'id':run_id})
        payload={'root_work_id':root_id,'context_sha256':prepared['context_sha256'],'decision':data['decision'],
                 'note':note,'run_id':run_id,'artifact_id':artifact_id}
        kernel._event(c,'policy.review_decided',review['work_id'],payload,actor='user',cause=snapshot['event_id'],
                    prior=review['version'],key=key)
        kernel._event(c,'run.verified',run_id,{'artifact_id':artifact_id,**result},actor='kernel',cause=key)
        return artifact_id,True


def reopen_policy_review(kernel, root_id, key):
    with kernel.engine.begin() as c:
        kernel._lock(c)
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
        kernel._transition(c,review['work_id'],'STALE',key)
        kernel._transition(c,review['work_id'],'WAITING_APPROVAL',key)
        kernel._event(c,'policy.review_reopened',review['work_id'],{'root_work_id':root_id,
                    'previous_artifact_id':str(review['accepted_artifact_id'])},actor='user',cause=key,
                    prior=review['version'],key=event_key)
        return True


def review_policy_note(kernel, work_id, request):
    """Record an explicit human observation; it does not approve or update policy."""
    data = request.model_dump()
    note = data['note'].strip()
    if not note:
        raise Conflict('review note must not be blank')
    with kernel.engine.begin() as c:
        kernel._lock(c)
        key = 'policy.review_note:'+data['idempotency_key']
        previous = one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        payload = {'artifact_id':data['artifact_id'],'note':note,'decision':'NOTE_ONLY'}
        if previous:
            if previous['subject_id'] != work_id or previous['payload'] != payload:
                raise Conflict('review note idempotency key already used')
            return False
        work = kernel._work(c,work_id)
        event = one(c,'SELECT event_id FROM policy_change_events WHERE root_work_id=:id',{'id':work_id})
        if not event or work['status'] != 'COMMITTED' or str(work['accepted_artifact_id']) != data['artifact_id']:
            raise Conflict('review note requires the current verified policy analysis')
        kernel._event(c,'policy.review_note',work_id,payload,actor='user',cause=event['event_id'],
                    prior=work['version'],key=key)
        return True


