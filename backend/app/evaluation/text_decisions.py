"""Human decisions on evaluated text methods; never promote automatically."""
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.evaluation.text_experiments import _active, _suite, _side_result, _targets, evaluation_pass
from app.work.market import digest
from app.work.service import Kernel, Conflict, NotFound, canonical, one, uid


class DecisionRequest(BaseModel):
    decision: Literal['APPLY', 'REJECT', 'ROLLBACK']
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=150)


def decide(db, experiment_id, body):
    note=body.note.strip()
    if not note:
        raise Conflict('a human decision note is required')
    kernel=Kernel(db); key='text-experiment.decision:'+body.idempotency_key
    request={'experiment_id':str(experiment_id),'decision':body.decision,'note':note}
    with db.begin() as c:
        kernel._lock(c)
        if one(c,'SELECT 1 FROM text_experiment_knowledge WHERE experiment_id=:id',{'id':experiment_id}):
            raise Conflict('knowledge comparisons must be applied through the candidate knowledge decision, not method promotion')
        previous=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request')!=request:
                raise Conflict('decision idempotency key already used')
            return False
        exp=one(c,'''SELECT x.*,r.result,r.result_sha256,r.verdict,g.generation AS experiment_generation
            FROM text_experiments x JOIN text_experiment_results r ON r.experiment_id=x.experiment_id
            JOIN text_experiment_generation g ON g.experiment_id=x.experiment_id WHERE x.experiment_id=:id''',{'id':experiment_id})
        if not exp or exp['status']!='EVALUATED':
            raise NotFound('completed experiment not found')
        current=_active(c,exp['task_id'])
        state=one(c,'SELECT generation FROM text_task_method_state WHERE task_id=:task FOR UPDATE',{'task':exp['task_id']})
        decisions=c.execute(text('SELECT * FROM text_experiment_decisions WHERE experiment_id=:id'),{'id':experiment_id}).mappings().all()
        by_kind={d['decision']:d for d in decisions}
        if body.decision in by_kind:
            raise Conflict('this decision has already been recorded')
        if digest(exp['result'])!=exp['result_sha256']:
            raise Conflict('evaluation result changed')
        from_version=current['version'];to_version=from_version;generation=state['generation']
        if body.decision=='REJECT':
            if by_kind:
                raise Conflict('an experiment decision already exists')
        elif body.decision=='APPLY':
            if by_kind or exp['verdict']!='PASS' or not evaluation_pass(exp['result']):
                raise Conflict('only an undecided passing comparison can be applied')
            if generation!=exp['experiment_generation'] or current['version']!=exp['champion_version']:
                raise Conflict('active method changed since the comparison')
            suite,snapshot=_suite(c,exp['task_id'],exp['suite_id'])
            candidate=one(c,'''SELECT revision,plan FROM work_design_revisions WHERE design_id=:id
                ORDER BY revision DESC LIMIT 1''',{'id':exp['design_id']})
            if (current['design_id']!=exp['design_id'] or digest(current['output_schema'])!=exp['champion_contract_sha256']
                or suite['snapshot_sha256']!=exp['suite_sha256'] or candidate['revision']!=exp['candidate_revision']
                or digest(candidate['plan'])!=exp['candidate_plan_sha256'] or digest(exp['candidate_plan'])!=exp['candidate_plan_sha256']):
                raise Conflict('champion, candidate or evaluation suite changed')
            saved={x['case_id']:x for x in exp['result']['cases']}
            if len(saved)!=len(snapshot['cases']):
                raise Conflict('evaluation cases changed')
            targets=_targets(c,exp,snapshot)
            if 'candidate_targets' in exp['result'] and (exp['result']['candidate_targets']!=targets['challenger']
                or exp['result'].get('champion_targets')!=targets['champion']
                or exp['result'].get('target_mapping_sha256')!=digest(targets)):
                raise Conflict('evaluation target mapping changed')
            for entry in snapshot['cases']:
                old=_side_result(c,exp,entry,'CHAMPION',current['output_schema'],targets)
                new=_side_result(c,exp,entry,'CHALLENGER',current['output_schema'],targets)
                recorded=saved.get(entry['case_id'],{})
                if old!=recorded.get('champion') or new!=recorded.get('challenger'):
                    raise Conflict('evaluation execution or evidence changed')
            contract=deepcopy(current['output_schema'])
            contract['plan']=exp['candidate_plan'];contract['design_revision']=exp['candidate_revision']
            registered=one(c,'''SELECT task_id,task_version FROM registered_work_designs
                WHERE design_id=:id AND revision=:revision''',{'id':exp['design_id'],'revision':exp['candidate_revision']})
            if registered:
                existing=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:task AND version=:version',
                             {'task':registered['task_id'],'version':registered['task_version']})
                if registered['task_id']!=exp['task_id'] or existing['output_schema']!=contract:
                    raise Conflict('candidate revision is already registered with different settings')
                to_version=registered['task_version']
            else:
                to_version=one(c,'SELECT MAX(version)+1 AS version FROM task_definitions WHERE task_id=:task',{'task':exp['task_id']})['version']
                c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
                    VALUES (:task,:version,:name,:objective,CAST(:input AS jsonb),CAST(:output AS jsonb),CAST(:policy AS jsonb),false)'''),
                    {'task':exp['task_id'],'version':to_version,'name':current['name'],'objective':current['objective'],
                     'input':canonical(current['input_schema']),'output':canonical(contract),'policy':canonical(current['acceptance_policy'])})
                c.execute(text('''INSERT INTO registered_work_designs(design_id,revision,task_id,task_version)
                    VALUES (:design,:revision,:task,:version)'''),{'design':exp['design_id'],'revision':exp['candidate_revision'],
                    'task':exp['task_id'],'version':to_version})
            generation+=1
        else:
            applied=by_kind.get('APPLY')
            if not applied or current['version']!=applied['to_version'] or generation!=applied['generation']:
                raise Conflict('this applied method is no longer the current version')
            to_version=applied['from_version'];generation+=1
        if body.decision in ('APPLY','ROLLBACK'):
            from app.work.procedural_knowledge import carry_forward_binding
            carry_forward_binding(kernel,c,exp['task_id'],current['version'],to_version)
            c.execute(text('UPDATE task_definitions SET active=false WHERE task_id=:task AND active'),{'task':exp['task_id']})
            c.execute(text('UPDATE task_definitions SET active=true WHERE task_id=:task AND version=:version'),{'task':exp['task_id'],'version':to_version})
            c.execute(text('UPDATE job_definitions SET task_version=:version WHERE job_id=:job'),{'version':to_version,'job':current['job_id']})
            c.execute(text('UPDATE text_task_method_state SET generation=:generation WHERE task_id=:task'),{'generation':generation,'task':exp['task_id']})
        decision_id=uid()
        c.execute(text('''INSERT INTO text_experiment_decisions(decision_id,experiment_id,decision,note,from_version,to_version,generation)
            VALUES (:id,:experiment,:decision,:note,:old,:new,:generation)'''),
            {'id':decision_id,'experiment':experiment_id,'decision':body.decision,'note':note,'old':from_version,'new':to_version,'generation':generation})
        kernel._event(c,'text_experiment.decision',str(experiment_id),{'request':request,'decision_id':decision_id,
            'result_sha256':exp['result_sha256'],'from_version':from_version,'to_version':to_version,'generation':generation},actor='user',key=key)
        return True
