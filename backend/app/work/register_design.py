"""Register a reviewed design as a version-pinned text-only task."""
import json
from uuid import uuid4
from typing import Literal
from croniter import croniter
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.work.designs import WorkPlan, validate_plan
from app.work.market import digest
from app.work.service import Kernel, Conflict, NotFound


class RegisterDesign(BaseModel):
    revision: int = Field(ge=1)
    source_text: str = Field(min_length=1, max_length=8000)
    model_mode: Literal['MOCK','LIVE_LLM'] = 'LIVE_LLM'
    schedule: str = Field(default='', max_length=100)


class ScheduleState(BaseModel):
    enabled: bool
    idempotency_key: str = Field(min_length=1, max_length=150)


def register(engine, design_id, request):
    if not request.source_text.strip():
        raise ValueError('source text is required')
    if request.schedule and (len(request.schedule.split()) != 5 or not croniter.is_valid(request.schedule)):
        raise ValueError('invalid five-field cron schedule')
    kernel = Kernel(engine)
    with engine.begin() as c:
        kernel._lock(c)
        parent = c.execute(text('SELECT design_id FROM work_designs WHERE design_id=CAST(:id AS uuid) FOR UPDATE'),{'id':design_id}).first()
        if not parent:
            raise NotFound('work design not found')
        row = c.execute(text('SELECT revision,plan FROM work_design_revisions WHERE design_id=CAST(:id AS uuid) ORDER BY revision DESC LIMIT 1'),{'id':design_id}).mappings().one()
        if row['revision'] != request.revision:
            raise Conflict('review the latest design revision before registering')
        plan = validate_plan(WorkPlan.model_validate(row['plan'])).model_dump()
        if (plan['expected_result'] == '사람이 기대 결과를 입력해야 합니다.' or
                '사람이 검증 가능한 기준을 추가해야 합니다.' in plan['evaluation_criteria']):
            raise ValueError('기대 결과와 검증 가능한 평가 기준을 직접 입력하고 저장하세요')
        contract = {'design_id':str(design_id),'design_revision':request.revision,'plan':plan,
                    'source_text':request.source_text,'model_mode':request.model_mode,
                    'execution_kind':'provided_text_workflow_v1'}
        existing = c.execute(text('''SELECT r.task_id,t.output_schema,j.schedule FROM registered_work_designs r
            JOIN task_definitions t ON t.task_id=r.task_id AND t.version=r.task_version
            JOIN job_definitions j ON j.task_id=r.task_id AND j.task_version=r.task_version
            WHERE r.design_id=CAST(:id AS uuid) AND r.revision=:revision'''),{'id':design_id,'revision':request.revision}).mappings().first()
        if existing:
            if existing['output_schema'] != contract or (existing['schedule'] or '') != request.schedule:
                raise Conflict('this revision is already registered with different execution settings')
            return {'task_id':existing['task_id'],'created':False}
        task_id = 'USER-'+str(uuid4())
        c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
            VALUES (:id,1,:name,:objective,CAST(:input AS jsonb),CAST(:contract AS jsonb),CAST(:policy AS jsonb),true)'''),
            {'id':task_id,'name':plan['name'],'objective':plan['objective'],
             'input':json.dumps({'kind':'provided_text','source_sha256':digest(request.source_text)},ensure_ascii=False),
             'contract':json.dumps(contract,ensure_ascii=False),
             'policy':json.dumps({'kind':'provided_text_workflow_v1','scope':'STRUCTURE_ONLY_NOT_SEMANTIC'})})
        c.execute(text('''INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type,schedule,timezone)
            VALUES (:job,:task,1,:trigger,:schedule,'Asia/Seoul')'''),
            {'job':task_id+'-JOB','task':task_id,'trigger':'CRON' if request.schedule else 'MANUAL','schedule':request.schedule or None})
        c.execute(text('INSERT INTO registered_work_designs(design_id,revision,task_id,task_version) VALUES (CAST(:design AS uuid),:revision,:task,1)'),
            {'design':design_id,'revision':request.revision,'task':task_id})
        kernel._event(c,'task.registered',task_id,{'design_id':str(design_id),'design_revision':request.revision,
            'contract_sha256':digest(contract),'source_sha256':digest(request.source_text),'schedule':request.schedule or None},actor='user')
        return {'task_id':task_id,'created':True}


def set_schedule(engine, task_id, request):
    kernel = Kernel(engine)
    key = 'schedule.state:' + request.idempotency_key
    payload = {'task_id':task_id,'enabled':request.enabled}
    with engine.begin() as c:
        kernel._lock(c)
        previous = c.execute(text('SELECT payload FROM work_events WHERE idempotency_key=:key'),{'key':key}).scalar_one_or_none()
        if previous is not None:
            if previous != payload:
                raise Conflict('schedule idempotency key already used')
            return False
        job = c.execute(text('''SELECT j.* FROM job_definitions j JOIN registered_work_designs r
            ON r.task_id=j.task_id AND r.task_version=j.task_version
            WHERE j.task_id=:task AND j.trigger_type='CRON' FOR UPDATE OF j'''),{'task':task_id}).mappings().first()
        if not job:
            raise NotFound('scheduled registered task not found')
        if job['enabled'] == request.enabled:
            return False
        c.execute(text('UPDATE job_definitions SET enabled=:enabled WHERE job_id=:job'),{'enabled':request.enabled,'job':job['job_id']})
        c.execute(text('DELETE FROM job_schedule_cursors WHERE job_id=:job'),{'job':job['job_id']})
        kernel._event(c,'job.schedule_state_changed',job['job_id'],payload,actor='user',key=key)
        return True
