"""Explicit human contract changes, separate from measured method improvement."""
from copy import deepcopy
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.domain.hashing import digest
from app.evaluation.text_experiments import _active, active_targets
from app.work.designs import WorkPlan, validate_plan
from app.work.service import Kernel, Conflict, NotFound, canonical, one, uid


class ContractChangeRequest(BaseModel):
    expected_version: int = Field(ge=1)
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=150)


class ContractRollbackRequest(BaseModel):
    expected_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=150)


def apply(db, task_id, body):
    note=body.note.strip()
    if not note: raise Conflict('a human decision note is required')
    request={'task_id':task_id,'expected_version':body.expected_version,'candidate_revision':body.candidate_revision,
             'candidate_sha256':body.candidate_sha256,'note':note}
    kernel=Kernel(db); key='text-contract.apply:'+body.idempotency_key
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request')!=request: raise Conflict('contract change idempotency key already used')
            return previous['subject_id'],False
        current=_active(c,task_id)
        if current['version']!=body.expected_version: raise Conflict('active contract changed')
        # Serialize against revise(), which locks this row before adding a revision.
        one(c,'SELECT design_id FROM work_designs WHERE design_id=:id FOR UPDATE',{'id':current['design_id']})
        candidate=one(c,'''SELECT revision,plan FROM work_design_revisions WHERE design_id=:design
            ORDER BY revision DESC LIMIT 1''',{'design':current['design_id']})
        if not candidate or candidate['revision']!=body.candidate_revision or candidate['revision']<=current['registered_revision']:
            raise Conflict('select the latest newer design revision')
        plan=validate_plan(WorkPlan.model_validate(candidate['plan'])).model_dump()
        if digest(plan)!=body.candidate_sha256: raise Conflict('candidate contract changed')
        old=current['output_schema']['plan']
        if any(plan[k]!=old[k] for k in ('steps','expected_result','open_questions')):
            raise Conflict('contract change may only change name, objective and evaluation criteria')
        if all(plan[k]==old[k] for k in ('name','objective','evaluation_criteria')):
            raise Conflict('no contract change to apply')
        targets=active_targets(c,task_id,current['version'])
        contract=deepcopy(current['output_schema']);contract['plan']=plan;contract['design_revision']=candidate['revision']
        registered=one(c,'''SELECT task_id,task_version FROM registered_work_designs
            WHERE design_id=:design AND revision=:revision''',{'design':current['design_id'],'revision':candidate['revision']})
        if registered:
            existing=one(c,'SELECT name,objective,output_schema FROM task_definitions WHERE task_id=:task AND version=:version',
                         {'task':registered['task_id'],'version':registered['task_version']})
            if (registered['task_id']!=task_id or existing['output_schema']!=contract
                or existing['name']!=plan['name'] or existing['objective']!=plan['objective']):
                raise Conflict('candidate revision is already registered with different settings')
            to_version=registered['task_version']
        else:
            to_version=one(c,'SELECT MAX(version)+1 AS version FROM task_definitions WHERE task_id=:task',{'task':task_id})['version']
            c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
                VALUES (:task,:version,:name,:objective,CAST(:input AS jsonb),CAST(:output AS jsonb),CAST(:policy AS jsonb),false)'''),
                {'task':task_id,'version':to_version,'name':plan['name'],'objective':plan['objective'],
                 'input':canonical(current['input_schema']),'output':canonical(contract),'policy':canonical(current['acceptance_policy'])})
            c.execute(text('''INSERT INTO registered_work_designs(design_id,revision,task_id,task_version)
                VALUES (:design,:revision,:task,:version)'''),{'design':current['design_id'],'revision':candidate['revision'],
                'task':task_id,'version':to_version})
        return _record(c,kernel,key,request,current,to_version,'APPLY',note,digest(plan),targets,None)


def rollback(db, change_id, body):
    note=body.note.strip()
    if not note: raise Conflict('a human decision note is required')
    request={'change_id':str(change_id),'expected_version':body.expected_version,'note':note}
    kernel=Kernel(db);key='text-contract.rollback:'+body.idempotency_key
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request')!=request: raise Conflict('contract rollback idempotency key already used')
            return previous['subject_id'],False
        change=one(c,"SELECT * FROM text_contract_changes WHERE change_id=:id AND decision='APPLY'",{'id':change_id})
        if not change: raise NotFound('contract change not found')
        current=_active(c,change['task_id'])
        state=one(c,'SELECT generation FROM text_task_method_state WHERE task_id=:task',{'task':change['task_id']})
        if (current['version']!=body.expected_version or current['version']!=change['to_version']
            or not state or state['generation']!=change['generation']):
            raise Conflict('this contract change is no longer current')
        if digest(current['output_schema']['plan'])!=change['plan_sha256'] or digest(change['targets'])!=change['targets_sha256']:
            raise Conflict('applied contract evidence changed')
        if one(c,'SELECT change_id FROM text_contract_changes WHERE reversed_change_id=:id',{'id':change_id}):
            raise Conflict('contract change already rolled back')
        target=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:task AND version=:version',
                   {'task':change['task_id'],'version':change['from_version']})
        if not target or digest(target['output_schema']['plan'])!=change['from_plan_sha256']:
            raise Conflict('previous contract changed')
        return _record(c,kernel,key,request,current,change['from_version'],'ROLLBACK',note,
                       digest(target['output_schema']['plan']),active_targets(c,change['task_id'],change['from_version']),str(change_id))


def _record(c,kernel,key,request,current,to_version,decision,note,plan_sha,targets,reversed_id):
    task=current['task_id']
    from app.work.procedural_knowledge import carry_forward_binding
    carry_forward_binding(kernel,c,task,current['version'],to_version)
    c.execute(text('INSERT INTO text_task_method_state(task_id) VALUES (:task) ON CONFLICT DO NOTHING'),{'task':task})
    generation=one(c,'UPDATE text_task_method_state SET generation=generation+1 WHERE task_id=:task RETURNING generation',{'task':task})['generation']
    c.execute(text('UPDATE task_definitions SET active=false WHERE task_id=:task AND active'),{'task':task})
    c.execute(text('UPDATE task_definitions SET active=true WHERE task_id=:task AND version=:version'),{'task':task,'version':to_version})
    c.execute(text('UPDATE job_definitions SET task_version=:version WHERE job_id=:job'),{'version':to_version,'job':current['job_id']})
    change_id=uid()
    from_plan_sha=digest(current['output_schema']['plan'])
    c.execute(text('''INSERT INTO text_contract_changes(change_id,task_id,decision,note,from_version,to_version,generation,
        from_plan_sha256,plan_sha256,targets,targets_sha256,reversed_change_id)
        VALUES (:id,:task,:decision,:note,:old,:new,:generation,:from_plan,:plan,CAST(:targets AS jsonb),:sha,:reversed)'''),
        {'id':change_id,'task':task,'decision':decision,'note':note,'old':current['version'],'new':to_version,
         'generation':generation,'from_plan':from_plan_sha,'plan':plan_sha,'targets':canonical(targets),'sha':digest(targets),'reversed':reversed_id})
    kernel._event(c,'text_contract.'+decision.lower(),change_id,{'request':request,'task_id':task,'from_version':current['version'],
        'to_version':to_version,'generation':generation,'from_plan_sha256':from_plan_sha,'plan_sha256':plan_sha,'targets_sha256':digest(targets)},actor='user',key=key)
    return change_id,True
