"""Paired replay of a frozen human-authored text suite against two plans."""
from copy import deepcopy
from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.work.designs import WorkPlan, validate_plan
from app.work.market import digest
from app.work.service import Kernel, Conflict, NotFound, canonical, one, uid
from app.evaluation.text_regression import verify_suite
from app.workloads.text_workflow import order_steps


class ExperimentRequest(BaseModel):
    suite_id: UUID
    candidate_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=150)


class FinalizeRequest(BaseModel):
    idempotency_key: str = Field(min_length=1,max_length=150)


def _active(c,task_id):
    row=one(c,'''SELECT t.*,r.design_id,r.revision AS registered_revision,j.job_id FROM task_definitions t
        JOIN registered_work_designs r ON r.task_id=t.task_id AND r.task_version=t.version
        JOIN job_definitions j ON j.task_id=t.task_id AND j.task_version=t.version
        WHERE t.task_id=:task AND t.active AND t.acceptance_policy->>'kind'='provided_text_workflow_v1' ''',{'task':task_id})
    if not row:
        raise NotFound('active registered text task not found')
    return row


def _suite(c,task_id,suite_id):
    suite=one(c,'SELECT * FROM text_regression_suites WHERE suite_id=:id AND task_id=:task',{'id':suite_id,'task':task_id})
    if not suite:
        raise NotFound('evaluation suite not found')
    snapshot=verify_suite(c,task_id,suite)
    latest=one(c,'SELECT MAX(version) AS version FROM text_regression_suites WHERE task_id=:task',{'task':task_id})['version']
    if latest!=suite['version']:
        raise Conflict('a newer evaluation suite exists')
    if len(snapshot['cases'])>10:
        raise Conflict('paired replay supports at most 10 cases per experiment')
    return suite,snapshot


def start(db,task_id,body):
    kernel=Kernel(db);key='text-experiment.start:'+body.idempotency_key
    request_hash=digest({'task_id':task_id,'suite_id':str(body.suite_id),'candidate_revision':body.candidate_revision})
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request_sha256')!=request_hash:
                raise Conflict('experiment idempotency key already used')
            return previous['subject_id'],False
        champion=_active(c,task_id);suite,snapshot=_suite(c,task_id,body.suite_id)
        candidate=one(c,'''SELECT revision,plan FROM work_design_revisions WHERE design_id=:design AND revision=:revision''',
                      {'design':champion['design_id'],'revision':body.candidate_revision})
        latest=one(c,'SELECT MAX(revision) AS revision FROM work_design_revisions WHERE design_id=:design',{'design':champion['design_id']})['revision']
        if not candidate or candidate['revision']<=champion['registered_revision'] or candidate['revision']!=latest:
            raise Conflict('select the latest newer design revision')
        plan=validate_plan(WorkPlan.model_validate(candidate['plan'])).model_dump()
        old=champion['output_schema']['plan']
        unchanged=('name','objective','expected_result','evaluation_criteria')
        if any(plan[k]!=old[k] for k in unchanged):
            raise Conflict('candidate may change only workflow steps; objective and evaluation criteria must remain fixed')
        if plan['steps']==old['steps']:
            raise Conflict('candidate workflow steps did not change')
        if {s['id'] for s in plan['steps']}!={s['id'] for s in old['steps']}:
            raise Conflict('paired replay currently requires the same step IDs')
        step_ids={s['id'] for s in plan['steps']}
        if any(entry['case']['step_id'] not in step_ids for entry in snapshot['cases']):
            raise Conflict('evaluation target step is missing from candidate')
        exp_id=uid()
        c.execute(text('''INSERT INTO text_experiments(experiment_id,task_id,champion_version,design_id,candidate_revision,suite_id,
            suite_sha256,champion_contract_sha256,candidate_plan,candidate_plan_sha256,status)
            VALUES (:id,:task,:version,:design,:revision,:suite,:suite_hash,:contract_hash,CAST(:plan AS jsonb),:plan_hash,'RUNNING')'''),
            {'id':exp_id,'task':task_id,'version':champion['version'],'design':champion['design_id'],'revision':candidate['revision'],
             'suite':body.suite_id,'suite_hash':suite['snapshot_sha256'],'contract_hash':digest(champion['output_schema']),
             'plan':canonical(plan),'plan_hash':digest(plan)})
        for entry in snapshot['cases']:
            for side in ('CHAMPION','CHALLENGER'):
                work_id=uid();input_snapshot={'evaluation_experiment_id':exp_id,'case_id':entry['case_id'],'side':side}
                c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status)
                    VALUES (:id,:job,:task,:version,CAST(:snapshot AS jsonb),'CREATED')'''),
                    {'id':work_id,'job':champion['job_id'],'task':task_id,'version':champion['version'],'snapshot':canonical(input_snapshot)})
                c.execute(text('''INSERT INTO text_experiment_work(experiment_id,case_id,side,work_id)
                    VALUES (:exp,CAST(:case AS uuid),:side,:work)'''),{'exp':exp_id,'case':entry['case_id'],'side':side,'work':work_id})
                kernel._event(c,'work.created',work_id,{'job_id':champion['job_id'],'input_snapshot':input_snapshot},actor='user',cause=exp_id)
                kernel._transition(c,work_id,'READY',exp_id)
                c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"),{'id':work_id})
        kernel._event(c,'text_experiment.started',exp_id,{'request_sha256':request_hash,'task_id':task_id,
            'suite_sha256':suite['snapshot_sha256'],'champion_contract_sha256':digest(champion['output_schema']),
            'candidate_plan_sha256':digest(plan),'case_count':len(snapshot['cases'])},actor='user',key=key)
        return exp_id,True


def _side_result(c,experiment,entry,side,base_contract):
    row=one(c,'''SELECT m.work_id,w.status AS work_status,r.run_id,r.status AS run_status,r.error,r.model_id,
        a.artifact_id,a.sha256,a.content,ctx.manifest,ctx.sha256 AS context_sha,e.result AS validation,e.input_refs AS evidence_refs
        FROM text_experiment_work m JOIN work_items w ON w.work_id=m.work_id
        LEFT JOIN LATERAL (SELECT * FROM runs WHERE work_id=w.work_id ORDER BY attempt_no DESC LIMIT 1) r ON true
        LEFT JOIN artifacts a ON a.run_id=r.run_id AND a.kind='provided_text_report'
        LEFT JOIN run_contexts ctx ON ctx.run_id=r.run_id
        LEFT JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='provided_text_structure'
        WHERE m.experiment_id=:exp AND m.case_id=CAST(:case AS uuid) AND m.side=:side''',
        {'exp':experiment['experiment_id'],'case':entry['case_id'],'side':side})
    if not row:
        raise Conflict('experiment work missing')
    if row['work_status'] in ('CREATED','READY','RUNNING','SUBMITTED','VERIFYING'):
        raise Conflict('paired executions are still running')
    result={'work_id':str(row['work_id']),'run_id':str(row['run_id']) if row['run_id'] else None,
            'run_status':row['run_status'],'artifact_id':str(row['artifact_id']) if row['artifact_id'] else None,
            'checks_passed':False,'valid_execution':False,'model_ids':[],'elapsed_ms':None,
            'input_tokens':None,'output_tokens':None,'missing_required':[],'found_forbidden':[]}
    if (row['work_status']!='WAITING_APPROVAL' or row['run_status']!='SUCCEEDED' or not isinstance(row['content'],dict)
        or not isinstance(row['manifest'],dict) or not isinstance(row['validation'],dict)
        or row['validation'].get('passed') is not True):
        return result
    content=row['content'];manifest=row['manifest'];scope=content.get('evaluation',{})
    expected_contract=deepcopy(base_contract)
    if side=='CHALLENGER':
        expected_contract['plan']=experiment['candidate_plan']
    expected_contract['source_text']=entry['case']['input_text']
    expected_scope={'experiment_id':str(experiment['experiment_id']),'suite_sha256':experiment['suite_sha256'],
        'case_id':entry['case_id'],'case_sha256':entry['case_sha256'],'side':side}
    refs=row['evidence_refs'] if isinstance(row['evidence_refs'],dict) else {}
    if (digest(content)!=row['sha256'] or digest(manifest)!=row['context_sha'] or scope!=expected_scope
        or manifest.get('evaluation')!=expected_scope or manifest.get('contract')!=expected_contract
        or content.get('contract_sha256')!=digest(expected_contract) or content.get('source_sha256')!=digest(entry['case']['input_text'])
        or refs.get('artifact_sha256')!=row['sha256'] or refs.get('contract_sha256')!=digest(expected_contract)
        or refs.get('source_sha256')!=digest(entry['case']['input_text'])
        or row['validation'].get('requires_human_review') is not True):
        raise Conflict('execution evidence or input snapshot changed')
    from app.workloads.text_workflow import verify as verify_text_output
    work=one(c,'SELECT * FROM work_items WHERE work_id=:id',{'id':row['work_id']})
    run=one(c,'SELECT * FROM runs WHERE run_id=:id',{'id':row['run_id']})
    task=one(c,'SELECT * FROM task_definitions WHERE task_id=:id AND version=:version',
             {'id':work['task_id'],'version':work['task_version']})
    rechecked,_=verify_text_output(c,dict(run),dict(work),dict(task),content,row['sha256'])
    if not rechecked['passed'] or rechecked.get('requires_human_review') is not True:
        raise Conflict('execution no longer passes independent structure checks')
    parts=content.get('steps',[])
    if not isinstance(parts,list) or any(not isinstance(x,dict) or not isinstance(x.get('step_id'),str) or not isinstance(x.get('model_id'),str) or not isinstance(x.get('text'),str) for x in parts):
        raise Conflict('execution output metadata is incomplete')
    if base_contract['model_mode']=='LIVE_LLM' and any(not isinstance(x.get('response_id'),str) or not x['response_id'] for x in parts):
        raise Conflict('live execution response provenance is missing')
    target=next((x for x in parts if x['step_id']==entry['case']['step_id']),None)
    if target is None:
        raise Conflict('evaluation target step missing')
    actual=target['text'].casefold()
    missing=[x for x in entry['case']['required_phrases'] if x.casefold() not in actual]
    found=[x for x in entry['case']['forbidden_phrases'] if x.casefold() in actual]
    def total(name):
        values=[x.get(name) for x in parts]
        return sum(values) if all(type(x) is int for x in values) else None
    result.update(valid_execution=True,checks_passed=not missing and not found,artifact_sha256=row['sha256'],
                  context_sha256=row['context_sha'],model_ids=sorted(set(x['model_id'] for x in parts)),
                  elapsed_ms=total('elapsed_ms'),input_tokens=total('input_tokens'),output_tokens=total('output_tokens'),
                  missing_required=missing,found_forbidden=found,target_excerpt=target['text'][:1000],
                  target_excerpt_truncated=len(target['text'])>1000,target_output_sha256=digest(target['text']))
    return result


def finish(db,experiment_id,body):
    kernel=Kernel(db);key='text-experiment.finish:'+body.idempotency_key
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['subject_id']!=str(experiment_id): raise Conflict('finish idempotency key already used')
            return False
        exp=one(c,'SELECT * FROM text_experiments WHERE experiment_id=:id FOR UPDATE',{'id':experiment_id})
        if not exp: raise NotFound('experiment not found')
        if exp['status']!='RUNNING': raise Conflict('experiment already evaluated')
        champion=_active(c,exp['task_id']);suite,snapshot=_suite(c,exp['task_id'],exp['suite_id'])
        latest_revision=one(c,'SELECT MAX(revision) AS revision FROM work_design_revisions WHERE design_id=:id',{'id':exp['design_id']})['revision']
        if (champion['version']!=exp['champion_version'] or digest(champion['output_schema'])!=exp['champion_contract_sha256']
            or suite['snapshot_sha256']!=exp['suite_sha256'] or digest(exp['candidate_plan'])!=exp['candidate_plan_sha256']
            or latest_revision!=exp['candidate_revision']):
            raise Conflict('champion, candidate or evaluation suite changed')
        cases=[]
        for entry in snapshot['cases']:
            old=_side_result(c,exp,entry,'CHAMPION',champion['output_schema']);new=_side_result(c,exp,entry,'CHALLENGER',champion['output_schema'])
            cases.append({'case_id':entry['case_id'],'case_sha256':entry['case_sha256'],'step_id':entry['case']['step_id'],
                          'expected_behavior':entry['case']['expected_behavior'],'champion':old,'challenger':new})
        complete=all(x['champion']['valid_execution'] and x['challenger']['valid_execution'] for x in cases)
        regressions=[x['case_id'] for x in cases if x['champion']['checks_passed'] and not x['challenger']['checks_passed']]
        gains=[x['case_id'] for x in cases if not x['champion']['checks_passed'] and x['challenger']['checks_passed']]
        models={m for x in cases for side in ('champion','challenger') for m in x[side]['model_ids']}
        live=champion['output_schema']['model_mode']=='LIVE_LLM'
        comparable=complete and len(models)==1 and 'MOCK' not in models
        gates={'all_executions_valid':complete,'same_live_model':live and comparable,'zero_regressions':not regressions,'at_least_one_gain':bool(gains)}
        result={'scope':'USER_DEFINED_SUBSTRING_CHECKS_NOT_SEMANTIC','suite_id':str(suite['suite_id']),
                'suite_sha256':suite['snapshot_sha256'],'champion_contract_sha256':exp['champion_contract_sha256'],
                'candidate_plan_sha256':exp['candidate_plan_sha256'],'case_count':len(cases),'cases':cases,
                'champion_passed':sum(x['champion']['checks_passed'] for x in cases),
                'challenger_passed':sum(x['challenger']['checks_passed'] for x in cases),
                'gains':gains,'regressions':regressions,'gates':gates,'model_ids':sorted(models),
                'semantic_quality':'NOT_EVALUATED','cost':'NOT_CALCULATED'}
        verdict='PASS' if all(gates.values()) else 'FAIL'
        c.execute(text('''INSERT INTO text_experiment_results(experiment_id,result,result_sha256,verdict)
            VALUES (:id,CAST(:result AS jsonb),:sha,:verdict)'''),{'id':experiment_id,'result':canonical(result),'sha':digest(result),'verdict':verdict})
        c.execute(text("UPDATE text_experiments SET status='EVALUATED' WHERE experiment_id=:id"),{'id':experiment_id})
        kernel._event(c,'text_experiment.evaluated',experiment_id,{'result_sha256':digest(result),'verdict':verdict,
            'suite_sha256':suite['snapshot_sha256'],'scope':result['scope']},actor='user',key=key)
        return True
