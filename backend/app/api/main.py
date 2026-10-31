from contextlib import asynccontextmanager
from decimal import Decimal
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.domain.contracts import TriggerRequest, DependencyRequest, RetryRequest, DocumentUpdatedRequest, FeedbackRequest, ActionRequest, PolicyReviewNoteRequest, PolicyReviewDecisionRequest
from app.work.db import engine, migrate
from app.work.seed import seed
from app.work.service import Kernel, Conflict, NotFound
from app.evaluation.improvements import ImprovementService
from app.integrations.search import readiness, SearchUnavailable


@asynccontextmanager
async def lifespan(app):
    migrate()
    seed(engine)
    yield


app = FastAPI(title='Work OS Kernel', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv('WORKOS_UI_ORIGIN','http://localhost:5173')], allow_methods=['*'], allow_headers=['*'])
kernel = Kernel(engine)
improvements = ImprovementService(engine)


@app.exception_handler(Conflict)
async def conflict_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409, content={'detail': str(exc)})


@app.exception_handler(NotFound)
async def missing_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={'detail': str(exc)})


@app.exception_handler(SearchUnavailable)
async def search_unavailable_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={'detail':'demo search is unavailable'})


@app.get('/api/health')
def health():
    with engine.connect() as c:
        c.execute(text('SELECT 1'))
    return {'mode':'MOCK', 'data_mode':'DEMO DATA', 'model_modes':['MOCK','LIVE_LLM'], 'database':'connected','search':readiness(),
            'workloads':{'TASK-001':'available', 'TASK-002':'available', 'TASK-003':'available'}, 'scheduler':'configured; process status not checked'}


@app.get('/api/tasks')
def tasks():
    with engine.connect() as c:
        tasks = [dict(r) for r in c.execute(text('''SELECT t.task_id,t.version,t.name,t.objective,j.job_id,j.trigger_type,j.schedule,j.event_filter,
          w.work_id,w.status,r.run_id,r.status AS run_status,r.error
          FROM task_definitions t JOIN job_definitions j ON j.task_id=t.task_id AND j.task_version=t.version
          LEFT JOIN LATERAL (SELECT * FROM work_items WHERE task_id=t.task_id AND parent_work_id IS NULL ORDER BY created_at DESC LIMIT 1) w ON true
          LEFT JOIN LATERAL (SELECT * FROM runs WHERE work_id=w.work_id ORDER BY attempt_no DESC LIMIT 1) r ON true
          WHERE t.active ORDER BY t.task_id''')).mappings()]
        for task in tasks:
            history = c.execute(text('''SELECT r.run_id,r.status,r.started_at,w.task_version,w.status AS work_status,
                  e.verdict,e.eval_suite_version,e.metric_results,a.artifact_version,
                  (w.status='COMMITTED' AND w.accepted_artifact_id=a.artifact_id) AS is_current
                FROM runs r JOIN work_items w ON w.work_id=r.work_id
                LEFT JOIN LATERAL (SELECT * FROM evaluations WHERE run_id=r.run_id ORDER BY created_at DESC LIMIT 1) e ON true
                LEFT JOIN artifacts a ON a.artifact_id=e.artifact_id
                WHERE w.task_id=:task_id AND w.parent_work_id IS NULL
                ORDER BY r.started_at DESC,r.run_id DESC LIMIT 5'''), {'task_id':task['task_id']}).mappings()
            task['recent_verifications'] = []
            for row in history:
                checks = (row['metric_results'] or {}).get('checks', {})
                checks = [value for value in checks.values() if isinstance(value, bool)]
                task['recent_verifications'].append({
                    'run_id':row['run_id'], 'run_status':row['status'], 'started_at':row['started_at'],
                    'task_version':row['task_version'], 'work_status':row['work_status'],
                    'verdict':row['verdict'], 'suite_version':row['eval_suite_version'],
                    'checks_passed':sum(checks), 'checks_total':len(checks),
                    'artifact_version':row['artifact_version'], 'is_current':bool(row['is_current']),
                })
        return tasks


@app.get('/api/jobs')
def jobs():
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text('SELECT * FROM job_definitions ORDER BY job_id')).mappings()]


@app.post('/api/tasks/{task_id}/run')
def trigger(task_id: str, body: TriggerRequest):
    if task_id == 'TASK-002':
        raise HTTPException(422, 'use the document.updated event to start policy analysis')
    if task_id == 'TASK-001':
        options = body.input_snapshot
        if options.get('dataset_version','v1') not in ('v1','v2') or options.get('model_mode','MOCK') not in ('MOCK','LIVE_LLM') or not isinstance(options.get('omit_document',False),bool):
            raise HTTPException(422, 'invalid demo dataset, model mode, or fault option')
    work, created = kernel.trigger(task_id+'-JOB', body.idempotency_key, body.input_snapshot)
    return {'work_id': work, 'created': created, 'data_mode': 'DEMO DATA', 'model_mode':body.input_snapshot.get('model_mode','MOCK')}


@app.get('/api/policies/{document_id}')
def policy(document_id: str):
    with engine.connect() as c:
        versions = c.execute(text('SELECT document_id,version,title,clauses,sha256 FROM policy_documents WHERE document_id=:doc ORDER BY version'), {'doc':document_id}).mappings().all()
        if not versions:
            raise HTTPException(404, 'policy document not found')
        dependencies = c.execute(text('SELECT * FROM task_source_dependencies WHERE document_id=:doc ORDER BY task_id'), {'doc':document_id}).mappings().all()
        event = c.execute(text('SELECT * FROM policy_change_events WHERE document_id=:doc ORDER BY created_at DESC LIMIT 1'), {'doc':document_id}).mappings().first()
        return {'data_mode':'DEMO DATA','versions':[dict(x) for x in versions],
                'dependencies':[dict(x) for x in dependencies],'latest_event':dict(event) if event else None}


@app.post('/api/events/document.updated')
def document_updated(body: DocumentUpdatedRequest):
    work_id,created = kernel.document_updated(body)
    return {'work_id':work_id,'created':created,'data_mode':'DEMO DATA'}


@app.get('/api/policy-work/{work_id}')
def policy_work(work_id: str):
    with engine.connect() as c:
        event = c.execute(text('SELECT * FROM policy_change_events WHERE root_work_id=:id'), {'id':work_id}).mappings().first()
        if not event:
            raise HTTPException(404, 'policy work not found')
        root = c.execute(text('SELECT * FROM work_items WHERE work_id=:id'), {'id':work_id}).mappings().one()
        children = c.execute(text('''SELECT w.work_id,w.status,f.step_no,f.title,d.predecessor_id,
            r.run_id,r.status AS run_status,r.error AS run_error,a.artifact_id,e.result AS verification,e.verifier_version FROM policy_followups f
            JOIN work_items w ON w.work_id=f.work_id LEFT JOIN work_dependencies d ON d.successor_id=f.work_id
            LEFT JOIN LATERAL (SELECT * FROM runs WHERE work_id=w.work_id ORDER BY attempt_no DESC LIMIT 1) r ON true
            LEFT JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id AND w.status='COMMITTED'
            LEFT JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type IN ('policy_followup_recheck','policy_index_recheck','policy_regression_check')
            WHERE f.root_work_id=:id ORDER BY f.step_no'''), {'id':work_id}).mappings().all()
        notes = c.execute(text("SELECT event_id,payload,created_at FROM work_events WHERE event_type='policy.review_note' AND subject_id=:id ORDER BY created_at,event_id"), {'id':work_id}).mappings().all()
        reviews = c.execute(text('''SELECT a.artifact_id,a.content,a.sha256,a.created_at,r.run_id,r.attempt_no,e.result,e.verifier_version,
            (w.status='COMMITTED' AND w.accepted_artifact_id=a.artifact_id) AS is_current
            FROM policy_followups f JOIN work_items w ON w.work_id=f.work_id JOIN runs r ON r.work_id=w.work_id
            JOIN artifacts a ON a.run_id=r.run_id AND a.kind='policy_human_review'
            JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='human_review_record'
            WHERE f.root_work_id=:id AND f.step_no=4 ORDER BY r.attempt_no DESC'''),{'id':work_id}).mappings().all()
        run = c.execute(text('SELECT * FROM runs WHERE work_id=:id ORDER BY attempt_no DESC LIMIT 1'), {'id':work_id}).mappings().first()
        artifact = None
        retrieval = None
        if run:
            artifact = c.execute(text('''SELECT a.*,e.result AS verification,e.verifier_version FROM artifacts a
                LEFT JOIN evidence e ON e.artifact_id=a.artifact_id WHERE a.run_id=:id ORDER BY a.created_at DESC LIMIT 1'''),
                {'id':run['run_id']}).mappings().first()
            retrieval = c.execute(text("SELECT manifest->'retrieval' FROM run_contexts WHERE run_id=:id"), {'id':run['run_id']}).scalar_one_or_none()
        return {'event':dict(event),'work':dict(root),'followups':[dict(x) for x in children],
                'run':dict(run) if run else None,'artifact':dict(artifact) if artifact else None,'retrieval':retrieval,
                'review_notes':[dict(note) for note in notes],'reviews':[dict(review) for review in reviews], 'data_mode':'DEMO DATA'}


@app.post('/api/policy-work/{work_id}/review-notes')
def policy_review_note(work_id: str, body: PolicyReviewNoteRequest):
    return {'recorded':kernel.review_policy_note(work_id,body),'decision':'NOTE_ONLY'}


@app.get('/api/policy-work/{work_id}/review-context')
def policy_review_context(work_id: str):
    try:
        return {'ready':True,**kernel.policy_review_context(work_id)}
    except Conflict as exc:
        return {'ready':False,'reason':str(exc)}
    except (SearchUnavailable,ValueError):
        return {'ready':False,'reason':'review inputs or search are unavailable'}


@app.post('/api/policy-work/{work_id}/review-decision')
def policy_review_decision(work_id: str, body: PolicyReviewDecisionRequest):
    artifact,created = kernel.decide_policy_review(work_id,body)
    return {'artifact_id':artifact,'recorded':created,'policy_applied':False}


@app.post('/api/policy-work/{work_id}/review-reopen')
def policy_review_reopen(work_id: str, body: ActionRequest):
    return {'reopened':kernel.reopen_policy_review(work_id,body.idempotency_key)}


@app.get('/api/voc/state')
def voc_state():
    with engine.connect() as c:
        state = c.execute(text("SELECT * FROM voc_method_state WHERE task_id='TASK-003'")).mappings().one()
        source = c.execute(text("SELECT * FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")).mappings().first()
        return {'data_mode':'DEMO DATA','model_mode':'MOCK','method':dict(state),'source_dependency':dict(source) if source else None}


@app.post('/api/feedback')
def voc_feedback(body: FeedbackRequest):
    proposal,created = improvements.feedback(body)
    return {'proposal_id':proposal,'created':created,'data_mode':'DEMO DATA'}


@app.get('/api/improvements')
def improvement_list():
    with engine.connect() as c:
        return [dict(row) for row in c.execute(text('''SELECT p.*,f.artifact_id,f.recommendation_id,f.reason,
            e.suite_sha256,e.champion_sha256,e.challenger_sha256,e.verdict,e.results
            FROM improvement_proposals p JOIN voc_feedback f ON f.feedback_id=p.feedback_id
            LEFT JOIN improvement_evaluations e ON e.evaluation_id=p.evaluation_id ORDER BY p.created_at DESC''')).mappings()]


@app.post('/api/improvements/{proposal_id}/evaluate')
def evaluate_improvement(proposal_id: str, body: ActionRequest):
    return {'proposal_id':proposal_id,'evaluated':improvements.evaluate(proposal_id,body.idempotency_key)}


@app.post('/api/improvements/{proposal_id}/{action}')
def decide_improvement(proposal_id: str, action: str, body: ActionRequest):
    if action not in ('apply','reject','rollback'):
        raise HTTPException(404,'unsupported action')
    return {'proposal_id':proposal_id,'changed':improvements.decide(proposal_id,action,body.idempotency_key)}


@app.get('/api/tasks/{task_id}/runs')
def task_runs(task_id: str):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text('''SELECT r.run_id,r.work_id,r.status,r.attempt_no,r.model_id,r.started_at,
            a.artifact_id,a.artifact_version,a.content,e.verdict FROM runs r JOIN work_items w ON w.work_id=r.work_id
            LEFT JOIN artifacts a ON a.run_id=r.run_id LEFT JOIN evaluations e ON e.artifact_id=a.artifact_id
            WHERE w.task_id=:task ORDER BY r.started_at DESC'''), {'task':task_id}).mappings()]


@app.get('/api/tasks/{task_id}/compare')
def compare(task_id: str, left: str, right: str):
    with engine.connect() as c:
        data = c.execute(text('''SELECT r.run_id,a.content FROM runs r JOIN work_items w ON w.work_id=r.work_id
            JOIN artifacts a ON a.run_id=r.run_id JOIN evaluations e ON e.artifact_id=a.artifact_id
            WHERE w.task_id=:task AND r.run_id IN (CAST(:left AS uuid),CAST(:right AS uuid)) AND e.verdict='PASS' AND r.status='SUCCEEDED' '''),
            {'task':task_id,'left':left,'right':right}).mappings().all()
    by_id = {str(r['run_id']):r['content'] for r in data}
    if left not in by_id or right not in by_id:
        raise HTTPException(404, 'two verified runs required')
    before, after = by_id[left], by_id[right]
    old = {x['symbol']:x for x in before['indicators']}
    changes = [{'symbol':x['symbol'],'before':old[x['symbol']]['current'],'after':x['current'],
                'difference':str(Decimal(x['current'])-Decimal(old[x['symbol']]['current']))} for x in after['indicators'] if x['symbol'] in old]
    return {'left':left,'right':right,'left_dataset':before['dataset_version'],'right_dataset':after['dataset_version'],
            'changes':changes,'left_interpretation':before['interpretation'],'right_interpretation':after['interpretation'],
            'interpretations_verified':False,'data_mode':'DEMO DATA'}


@app.post('/api/work/{work_id}/retry')
def retry(work_id: str, body: RetryRequest):
    return {'work_id': work_id, 'queued': kernel.retry(work_id, body.idempotency_key)}


@app.post('/api/work/{work_id}/revalidate')
def revalidate(work_id: str, body: RetryRequest):
    return {'work_id':work_id,'queued':kernel.revalidate(work_id,body.idempotency_key)}


@app.post('/api/dependencies')
def dependency(body: DependencyRequest):
    kernel.add_dependency(body.predecessor_id, body.successor_id, body.dependency_kind)
    return {'created': True}


@app.get('/api/work/{work_id}')
def work_detail(work_id: str):
    with engine.connect() as c:
        work = c.execute(text('SELECT * FROM work_items WHERE work_id=:id'), {'id': work_id}).mappings().first()
        if not work:
            raise HTTPException(404, 'work item not found')
        runs = c.execute(text('SELECT * FROM runs WHERE work_id=:id ORDER BY attempt_no'), {'id': work_id}).mappings().all()
        edges = c.execute(text('SELECT * FROM work_dependencies WHERE predecessor_id=:id OR successor_id=:id'), {'id': work_id}).mappings().all()
        events = c.execute(text('SELECT * FROM work_events WHERE subject_id=:id OR subject_id IN (SELECT CAST(run_id AS text) FROM runs WHERE work_id=CAST(:id AS uuid)) ORDER BY created_at'), {'id': work_id}).mappings().all()
        return {'work': dict(work), 'runs': [dict(r) for r in runs], 'dependencies': [dict(e) for e in edges], 'events': [dict(e) for e in events]}


@app.get('/api/runs/{run_id}')
def run_detail(run_id: str):
    with engine.connect() as c:
        run = c.execute(text('SELECT * FROM runs WHERE run_id=:id'), {'id': run_id}).mappings().first()
        if not run:
            raise HTTPException(404, 'run not found')
        context = c.execute(text('SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id'), {'id':run_id}).mappings().first()
        steps = c.execute(text('SELECT graph_version,node_name,status,error_type,created_at FROM run_steps WHERE run_id=:id ORDER BY step_id'), {'id':run_id}).mappings().all()
        return {**dict(run), 'context':dict(context) if context else None, 'steps':[dict(step) for step in steps]}


@app.get('/api/runs/{run_id}/artifacts')
def artifacts(run_id: str):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text('''SELECT a.*, COALESCE(jsonb_agg(to_jsonb(e)) FILTER (WHERE e.evidence_id IS NOT NULL),'[]') AS evidence
            FROM artifacts a LEFT JOIN evidence e ON e.artifact_id=a.artifact_id WHERE a.run_id=:id GROUP BY a.artifact_id'''), {'id': run_id}).mappings()]
