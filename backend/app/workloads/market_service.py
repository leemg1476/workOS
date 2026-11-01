"""Market demo input and model provenance assembly."""
from sqlalchemy import text
from app.work.market import build_manifest, digest, PROMPT_VERSION
from app.work.service import Conflict, canonical, one

def prepare_market(kernel, run_id):
    with kernel.engine.connect() as c:
        run = one(c, """SELECT r.work_id,r.attempt_no,w.task_id,w.task_version,w.input_snapshot FROM runs r
            JOIN work_items w ON w.work_id=r.work_id WHERE r.run_id=:id AND r.status='RUNNING'""", {'id':run_id})
        if not run or run['task_id'] != 'TASK-001' or run['task_version'] != 2:
            raise Conflict('run is not an eligible market task')
        snapshot = dict(run['input_snapshot'])
        # Fault injection affects only the first attempt; retry reconstructs full sources.
        if run['attempt_no'] > 1:
            snapshot['omit_document'] = False
        manifest = build_manifest(kernel.engine, run_id, run['work_id'], run['task_version'], snapshot)
    with kernel.engine.begin() as c:
        kernel._lock(c)
        active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
        if not active:
            raise Conflict('run lease expired')
        c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                  {'id':run_id, 'manifest':canonical(manifest), 'sha':digest(manifest)})
        kernel._event(c, 'run.context_created', run_id, {'manifest_sha256':digest(manifest), 'dataset_version':manifest['dataset_version']})
    return manifest


def record_market_model(kernel, run_id, metadata):
    with kernel.engine.begin() as c:
        kernel._lock(c)
        active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
        if not active:
            raise Conflict('run lease expired')
        c.execute(text("UPDATE runs SET agent_version=:agent,model_id=:model,prompt_version=:prompt WHERE run_id=:id AND status='RUNNING'"),
                  {'agent':'market-'+metadata['model_mode'], 'model':metadata['model_id'], 'prompt':PROMPT_VERSION, 'id':run_id})
        kernel._event(c, 'run.model_completed', run_id, metadata)


def record_market_model_started(kernel, run_id, mode, model):
    if mode not in ('MOCK','LIVE_LLM'):
        raise ValueError('unsupported model mode')
    with kernel.engine.begin() as c:
        kernel._lock(c)
        active = one(c, "SELECT 1 FROM work_queue WHERE run_id=:id AND status='CLAIMED' AND lease_until > now()", {'id':run_id})
        if not active:
            raise Conflict('run lease expired')
        c.execute(text("UPDATE runs SET agent_version=:agent,model_id=:model,prompt_version=:prompt WHERE run_id=:id AND status='RUNNING'"),
                  {'agent':'market-'+mode, 'model':model, 'prompt':PROMPT_VERSION, 'id':run_id})
        kernel._event(c, 'run.model_started', run_id, {'model_mode':mode,'model_id':model,'prompt_version':PROMPT_VERSION})


