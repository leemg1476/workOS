"""Evidence-linked task memory. History is data, never instructions or new facts."""
from sqlalchemy import text
from app.domain.hashing import digest, canonical

VERSION = 'task-memory-v3'


def prompt_view(manifest):
    """AI critiques remain inspectable/proposed, never become operational instructions."""
    view=dict(manifest)
    reviews=manifest.get('reviews',[])
    view['reviews']=[r for r in reviews if r.get('scope')=='HUMAN_JUDGMENT']
    view['unapplied_ai_reviews_omitted']=sum(r.get('scope')!='HUMAN_JUDGMENT' for r in reviews)
    view['policy']='APPLIED_KNOWLEDGE_AND_HUMAN_FEEDBACK; AI_SUGGESTIONS_REQUIRE_REVIEW'
    return view


def build(c, task_id, task_version, *, exclude_run=None, evaluation=False):
    memory = {'version': VERSION, 'task_id': task_id, 'task_version': task_version,
              'scope': 'SAME_TASK_VERSION_HISTORY_NOT_CURRENT_SOURCE_FACTS',
              'evaluation_isolated': evaluation, 'accepted_results': [], 'draft_results': [], 'failures': [], 'reviews': []}
    if evaluation:
        return memory
    from app.work.knowledge import active_snapshot,source_text
    source=source_text(c,task_id,task_version)
    input_unavailable=False
    if exclude_run:
        work=c.execute(text('SELECT w.* FROM work_items w JOIN runs r USING(work_id) WHERE r.run_id=:id'),{'id':exclude_run}).mappings().one()
        task=c.execute(text('SELECT output_schema,acceptance_policy FROM task_definitions WHERE task_id=:id AND version=:v'),{'id':task_id,'v':task_version}).mappings().one()
        if task['acceptance_policy'].get('kind')=='provided_text_workflow_v1':
            from app.evaluation.text_runtime import effective_contract
            from app.work.service import Conflict
            try:
                contract,_=effective_contract(c,work['work_id'],task_id,task_version,task['output_schema'])
                source=contract['source_text']
            except Conflict:
                # Preserve a claim/Run so the executor can record its input failure.
                # Do not supply trusted knowledge from the latest, unpinned input.
                input_unavailable=True
    memory['knowledge']=active_snapshot(c,task_id,task_version,source) if not input_unavailable else {
        'version':'task-knowledge-v1','active':[],'excluded':[],'sha256':digest([]),'unavailable':'WORK_INPUT_NOT_VERIFIED'}
    library=[] if input_unavailable else c.execute(text('''SELECT DISTINCT ON (s.url) s.snapshot_id,s.url,s.text_content,s.text_sha256,s.captured_at
        FROM report_source_snapshots s JOIN runs r USING(run_id) JOIN work_items w USING(work_id)
        JOIN run_contexts ctx ON ctx.run_id=r.run_id
        WHERE w.task_id=:task AND w.task_version=:v AND ctx.manifest->>'source_sha256'=:scope
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY s.url,s.captured_at DESC,s.snapshot_id DESC'''),{'task':task_id,'v':task_version,'scope':digest(source)}).mappings().all()
    memory['source_library']=[{'snapshot_id':str(s['snapshot_id']),'url':s['url'],'text_sha256':s['text_sha256'],
        'excerpt':s['text_content'][:6000],'truncated':len(s['text_content'])>6000,
        'scope':'CAPTURED_SOURCE_DATA_NOT_VERIFIED_INTERPRETATION'} for s in sorted(library,key=lambda s:s['captured_at'],reverse=True)[:3]]
    params = {'task': task_id, 'version': task_version, 'exclude': exclude_run}
    accepted = c.execute(text('''SELECT w.work_id,a.artifact_id,a.run_id,a.sha256,a.content,
        e.evidence_id,e.verifier_version,e.result,r.model_id
        FROM work_items w JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id
        JOIN runs r ON r.run_id=a.run_id
        JOIN LATERAL (SELECT * FROM evidence ev WHERE ev.artifact_id=a.artifact_id
          AND ev.result->>'passed'='true' ORDER BY ev.created_at DESC,ev.evidence_id DESC LIMIT 1) e ON true
        WHERE w.task_id=:task AND w.task_version=:version AND w.status='COMMITTED'
          AND r.status='SUCCEEDED' AND (CAST(:exclude AS uuid) IS NULL OR r.run_id<>CAST(:exclude AS uuid))
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 2'''), params).mappings()
    for row in accepted:
        if digest(row['content']) != row['sha256']:
            continue
        excerpt='\n\n'.join(step.get('text','') for step in row['content'].get('steps',[]) if isinstance(step,dict)) or canonical(row['content'])
        memory['accepted_results'].append({
            **{key: str(row[key]) for key in ('work_id','artifact_id','run_id','evidence_id')},
            'sha256': row['sha256'], 'verifier_version': row['verifier_version'],
            'model_id': row['model_id'], 'semantic_review': row['content'].get('semantic_review','SEE_EVIDENCE'),
            'excerpt': excerpt[:2000], 'truncated': len(excerpt) > 2000})
    failures = c.execute(text('''SELECT r.run_id,r.work_id,r.status,r.error FROM runs r
        JOIN work_items w ON w.work_id=r.work_id
        WHERE w.task_id=:task AND w.task_version=:version
          AND r.status IN ('FAILED','VERIFICATION_FAILED')
          AND (CAST(:exclude AS uuid) IS NULL OR r.run_id<>CAST(:exclude AS uuid))
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY r.started_at DESC,r.run_id DESC LIMIT 3'''),params).mappings()
    memory['failures'] = [{'run_id':str(r['run_id']), 'work_id':str(r['work_id']),
                           'status':r['status'], 'reason':(r['error'] or '')[:1200]} for r in failures]
    drafts=c.execute(text('''SELECT w.work_id,a.artifact_id,a.run_id,a.content,a.sha256 FROM artifacts a
        JOIN runs r ON r.run_id=a.run_id JOIN work_items w ON w.work_id=r.work_id
        WHERE w.task_id=:task AND w.task_version=:version AND w.status='WAITING_APPROVAL' AND r.status='SUCCEEDED'
          AND (CAST(:exclude AS uuid) IS NULL OR r.run_id<>CAST(:exclude AS uuid))
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 2'''),params).mappings()
    for row in drafts:
        if digest(row['content'])!=row['sha256']: continue
        excerpt='\n\n'.join(s.get('text','') for s in row['content'].get('steps',[]) if isinstance(s,dict))
        memory['draft_results'].append({**{k:str(row[k]) for k in ('work_id','artifact_id','run_id')},
            'sha256':row['sha256'],'excerpt':excerpt[:2000],'truncated':len(excerpt)>2000,
            'scope':'UNACCEPTED_DRAFT_NOT_VERIFIED_FACTS'})
    reviews = c.execute(text('''SELECT ev.event_id,ev.payload FROM work_events ev
        JOIN work_items w ON ev.subject_id=w.work_id::text
        WHERE ev.event_type IN ('work.reviewed','work.agent_feedback') AND w.task_id=:task AND w.task_version=:version
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY ev.created_at DESC,ev.event_id DESC LIMIT 3'''),params).mappings()
    memory['reviews'] = [{'event_id':str(r['event_id']), 'work_id':r['payload']['work_id'],
                          'artifact_id':r['payload']['artifact_id'], 'decision':r['payload']['decision'],
                          'note':r['payload']['note'][:1200], 'scope':r['payload'].get('scope','HUMAN_JUDGMENT')} for r in reviews]
    return memory


def pin(kernel, c, run_id, work):
    evaluation = c.execute(text('SELECT 1 FROM text_experiment_work WHERE work_id=:id'),
                           {'id':work['work_id']}).first() is not None
    manifest = build(c, work['task_id'], work['task_version'], exclude_run=run_id, evaluation=evaluation)
    sha = digest(manifest)
    c.execute(text('INSERT INTO run_task_memories(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
              {'id':run_id, 'manifest':canonical(manifest), 'sha':sha})
    kernel._event(c, 'run.memory_pinned', run_id, {'memory_sha256':sha, 'memory_version':VERSION})


def load(c, run_id):
    row = c.execute(text('SELECT manifest,sha256 FROM run_task_memories WHERE run_id=:id'),
                    {'id':run_id}).mappings().first()
    if not row or digest(row['manifest']) != row['sha256']:
        raise ValueError('task memory snapshot missing or changed')
    return dict(row)


def overview(c, task_id):
    from app.work.service import NotFound
    task = c.execute(text('SELECT version FROM task_definitions WHERE task_id=:id AND active'),
                     {'id':task_id}).mappings().first()
    if not task:
        raise NotFound('active task not found')
    memory = build(c, task_id, task['version'])
    works = [dict(r) for r in c.execute(text('''SELECT w.work_id,w.task_version,w.status,w.accepted_artifact_id,
        r.run_id,r.status AS run_status,r.error FROM work_items w
        LEFT JOIN LATERAL (SELECT * FROM runs WHERE work_id=w.work_id ORDER BY attempt_no DESC LIMIT 1) r ON true
        WHERE w.task_id=:id AND w.parent_work_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
        ORDER BY w.created_at DESC,w.work_id DESC LIMIT 20'''),{'id':task_id}).mappings()]
    actions = {'READY':'QUEUED','RUNNING':'IN_PROGRESS','WAITING_APPROVAL':'REVIEW',
               'REWORK_REQUIRED':'RETRY','STALE':'REVALIDATE','BLOCKED':'CHECK_DEPENDENCIES',
               'COMMITTED':'RESULT_AVAILABLE','CANCELLED':'CANCELLED'}
    for work in works:
        work['next_action'] = actions.get(work['status'],'CHECK_STATUS')
    return {'task_id':task_id, 'task_version':task['version'], 'memory':memory,
            'memory_sha256':digest(memory), 'recent_work':works,
            'continuation_policy':'LEASE_RECOVERY; TRANSIENT_EXECUTION_ERRORS_MAX_3_ATTEMPTS; OTHER_FAILURES_MANUAL',
            'improvement_policy':'FIXED_EVALUATION_AND_EXPLICIT_APPLY'}
