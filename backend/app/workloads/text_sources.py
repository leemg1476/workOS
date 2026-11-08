"""Version and pin user-provided inputs separately from a text task's method."""
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.work.market import digest
from app.work.service import Kernel, Conflict, NotFound, one
from app.workloads.registry import register_trigger_hook


class UpdateTextSource(BaseModel):
    source_text: str = Field(min_length=1,max_length=8000)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=150)


def _task(c, task_id):
    task=one(c,'''SELECT output_schema FROM task_definitions WHERE task_id=:task AND active
                  AND acceptance_policy->>'kind'='provided_text_workflow_v1' ''',{'task':task_id})
    if not task:
        raise NotFound('active text task not found')
    return task


def _current(c, task_id):
    task=_task(c,task_id)
    row=one(c,'''SELECT version,source_text,source_sha256,created_at FROM text_source_versions
        WHERE task_id=:task ORDER BY version DESC LIMIT 1''',{'task':task_id})
    if row:
        if digest(row['source_text'])!=row['source_sha256']:
            raise Conflict('stored source hash changed')
        return dict(row)
    source=task['output_schema']['source_text']
    return {'version':1,'source_text':source,'source_sha256':digest(source),'created_at':None}


def _ensure(c,task_id):
    current=_current(c,task_id)
    if current['version']==1:
        c.execute(text('''INSERT INTO text_source_versions(task_id,version,source_text,source_sha256)
            VALUES (:task,1,:source,:sha) ON CONFLICT (task_id,version) DO NOTHING'''),
            {'task':task_id,'source':current['source_text'],'sha':current['source_sha256']})
    return current


def get_source(db, task_id):
    with db.connect() as c:
        return _current(c,task_id)


def update_source(db,task_id,body):
    if not body.source_text.strip():
        raise ValueError('source text is required')
    kernel=Kernel(db);key='text-source.update:'+body.idempotency_key
    request_hash=digest({'task_id':task_id,'expected_version':body.expected_version,'source_text':body.source_text})
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['subject_id']!=task_id or previous['payload'].get('request_sha256')!=request_hash:
                raise Conflict('source idempotency key already used')
            return previous['payload']['version'],False
        current=_ensure(c,task_id)
        if current['version']!=body.expected_version:
            raise Conflict('source changed; refresh before editing')
        if current['source_text']==body.source_text:
            raise Conflict('source text did not change')
        version=current['version']+1;sha=digest(body.source_text)
        c.execute(text('''INSERT INTO text_source_versions(task_id,version,source_text,source_sha256)
            VALUES (:task,:version,:source,:sha)'''),{'task':task_id,'version':version,'source':body.source_text,'sha':sha})
        kernel._event(c,'text_source.updated',task_id,{'version':version,'source_sha256':sha,'request_sha256':request_hash},actor='user',key=key)
        return version,True


def pin_source(kernel,c,job,work_id):
    current=_ensure(c,job['task_id'])
    c.execute(text('''INSERT INTO text_source_work(work_id,task_id,source_version)
        VALUES (:work,:task,:version)'''),{'work':work_id,'task':job['task_id'],'version':current['version']})
    kernel._event(c,'text_source.pinned',work_id,{'task_id':job['task_id'],'version':current['version'],
        'source_sha256':current['source_sha256']})
    from app.evaluation.memory_study import freeze_work
    freeze_work(kernel,c,job,work_id)


register_trigger_hook('provided_text_workflow_v1',pin_source)
