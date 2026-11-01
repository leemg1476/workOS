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
    champion_targets: dict[str,str] = Field(default_factory=dict,max_length=50)
    candidate_targets: dict[str,str] = Field(default_factory=dict,max_length=50)
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
    required=set()
    prior=c.execute(text('SELECT * FROM text_regression_suites WHERE task_id=:task AND version<:version'),
                    {'task':task_id,'version':suite['version']}).mappings().all()
    for older in prior:
        required.update(x['case_id'] for x in verify_suite(c,task_id,older)['cases'])
    if not required.issubset({x['case_id'] for x in snapshot['cases']}):
        raise Conflict('evaluation suite omits a previously frozen case')
    if len(snapshot['cases'])>50:
        raise Conflict('paired replay supports at most 50 cases per experiment')
    return suite,snapshot


def active_targets(c,task_id,task_version):
    """Find case targets recorded when the active method version was last applied."""
    row=one(c,'''SELECT value,sha,kind FROM (
        SELECT targets AS value,targets_sha256 AS sha,generation,'contract' AS kind FROM text_contract_changes
        WHERE task_id=:task AND decision='APPLY' AND to_version=:version
        UNION ALL
        SELECT r.result AS value,r.result_sha256 AS sha,d.generation,'experiment' AS kind FROM text_experiment_decisions d
        JOIN text_experiments x ON x.experiment_id=d.experiment_id
        JOIN text_experiment_results r ON r.experiment_id=x.experiment_id
        WHERE x.task_id=:task AND d.decision='APPLY' AND d.to_version=:version) history
        ORDER BY generation DESC LIMIT 1''',{'task':task_id,'version':task_version})
    if not row:
        return {}
    if not isinstance(row['value'],dict) or digest(row['value'])!=row['sha']:
        raise Conflict('applied evaluation or contract mapping changed')
    targets=row['value'] if row['kind']=='contract' else row['value'].get('candidate_targets',{})
    if not isinstance(targets,dict):
        raise Conflict('applied evaluation target mapping is invalid')
    return targets


def _targets(c, experiment, snapshot):
    """Resolve immutable, human-selected case targets; old comparisons use their original step IDs."""
    row=one(c,'SELECT targets,targets_sha256 FROM text_experiment_targets WHERE experiment_id=:id',
            {'id':experiment['experiment_id']})
    cases=snapshot['cases']
    if row:
        targets=row['targets']
        if not isinstance(targets,dict) or digest(targets)!=row['targets_sha256']:
            raise Conflict('evaluation target mapping changed')
    else:
        created_after=one(c,'''SELECT x.created_at>=m.applied_at AS required FROM text_experiments x
            JOIN schema_migrations m ON m.version='018_text_experiment_targets' WHERE x.experiment_id=:id''',
            {'id':experiment['experiment_id']})
        if created_after and created_after['required']:
            raise Conflict('evaluation target mapping missing')
        identity={x['case_id']:x['case']['step_id'] for x in cases}
        targets={'champion':identity,'challenger':identity}
    case_ids={x['case_id'] for x in cases}
    if (set(targets)!={'champion','challenger'} or any(not isinstance(targets.get(side),dict)
        or set(targets[side])!=case_ids or any(not isinstance(v,str) or not v for v in targets[side].values())
        for side in ('champion','challenger'))):
        raise Conflict('evaluation target mapping incomplete')
    champion=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:task AND version=:version',
                 {'task':experiment['task_id'],'version':experiment['champion_version']})
    if not champion or not set(targets['champion'].values())<={s['id'] for s in champion['output_schema']['plan']['steps']}:
        raise Conflict('evaluation target step missing from champion')
    if not set(targets['challenger'].values())<={s['id'] for s in experiment['candidate_plan']['steps']}:
        raise Conflict('evaluation target step missing from candidate')
    return targets


def evaluation_pass(result):
    gates=result.get('gates',{})
    common=('all_executions_valid','same_live_model','zero_regressions')
    if not all(gates.get(k) is True for k in common):
        return False
    if result.get('change_kind','METHOD') in ('CRITERIA_ADDITION','EXPECTED_RESULT_CHANGE'):
        return (gates.get('all_cases_pass') is True and gates.get('added_criteria_cases_pass') is True
                and gates.get('expected_result_cases_pass',True) is True)
    return result.get('change_kind','METHOD')=='METHOD' and gates.get('at_least_one_gain') is True


def start(db,task_id,body):
    kernel=Kernel(db);key='text-experiment.start:'+body.idempotency_key
    request={'task_id':task_id,'suite_id':str(body.suite_id),'candidate_revision':body.candidate_revision}
    if body.champion_targets:
        request['champion_targets']=body.champion_targets
    if body.candidate_targets:
        request['candidate_targets']=body.candidate_targets
    request_hash=digest(request)
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
        unchanged=('name','objective')
        if any(plan[k]!=old[k] for k in unchanged):
            raise Conflict('task name and objective must remain fixed')
        old_criteria=old['evaluation_criteria'];criteria=plan['evaluation_criteria']
        if criteria[:len(old_criteria)]!=old_criteria:
            raise Conflict('existing evaluation criteria cannot be removed or changed')
        added=criteria[len(old_criteria):]
        expected_changed=plan['expected_result']!=old['expected_result']
        steps_changed=plan['steps']!=old['steps']
        if sum((bool(added),expected_changed,steps_changed))>1:
            raise Conflict('change only one of workflow steps, expected result or evaluation criteria in one comparison')
        if not any((added,expected_changed,steps_changed)):
            raise Conflict('candidate workflow and criteria did not change')
        case_expectations={entry['case']['expected_behavior'] for entry in snapshot['cases']}
        if any(criterion not in case_expectations for criterion in added):
            raise Conflict('each added criterion requires a matching expected behavior in the frozen suite')
        if expected_changed and not {old['expected_result'],plan['expected_result']}<=case_expectations:
            raise Conflict('old and new expected results each require a matching frozen case')
        step_ids={s['id'] for s in plan['steps']};old_ids={s['id'] for s in old['steps']}
        case_ids={entry['case_id'] for entry in snapshot['cases']}
        if not set(body.candidate_targets)<=case_ids or not set(body.champion_targets)<=case_ids:
            raise Conflict('candidate target references a case outside the evaluation suite')
        baseline=active_targets(c,task_id,champion['version'])
        champion_targets={}
        for entry in snapshot['cases']:
            case_id=entry['case_id'];known=baseline.get(case_id,entry['case']['step_id'])
            requested=body.champion_targets.get(case_id,known)
            if known in old_ids and requested!=known:
                raise Conflict('an existing champion target cannot be changed')
            if requested not in old_ids:
                raise Conflict('select an existing champion step for every evaluation case')
            champion_targets[case_id]=requested
        candidate_targets={entry['case_id']:body.candidate_targets.get(entry['case_id'],champion_targets[entry['case_id']]) for entry in snapshot['cases']}
        if any(not value or value not in step_ids for value in candidate_targets.values()):
            raise Conflict('select an existing candidate step for every evaluation case')
        targets={'champion':champion_targets,'challenger':candidate_targets}
        exp_id=uid()
        c.execute(text('''INSERT INTO text_experiments(experiment_id,task_id,champion_version,design_id,candidate_revision,suite_id,
            suite_sha256,champion_contract_sha256,candidate_plan,candidate_plan_sha256,status)
            VALUES (:id,:task,:version,:design,:revision,:suite,:suite_hash,:contract_hash,CAST(:plan AS jsonb),:plan_hash,'RUNNING')'''),
            {'id':exp_id,'task':task_id,'version':champion['version'],'design':champion['design_id'],'revision':candidate['revision'],
             'suite':body.suite_id,'suite_hash':suite['snapshot_sha256'],'contract_hash':digest(champion['output_schema']),
             'plan':canonical(plan),'plan_hash':digest(plan)})
        c.execute(text('''INSERT INTO text_experiment_targets(experiment_id,targets,targets_sha256)
            VALUES (:id,CAST(:targets AS jsonb),:sha)'''),{'id':exp_id,'targets':canonical(targets),'sha':digest(targets)})
        c.execute(text('INSERT INTO text_task_method_state(task_id) VALUES (:task) ON CONFLICT DO NOTHING'),{'task':task_id})
        c.execute(text('''INSERT INTO text_experiment_generation(experiment_id,generation)
            SELECT :id,generation FROM text_task_method_state WHERE task_id=:task'''),{'id':exp_id,'task':task_id})
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
            'candidate_plan_sha256':digest(plan),'targets_sha256':digest(targets),'case_count':len(snapshot['cases'])},actor='user',key=key)
        return exp_id,True


def _side_result(c,experiment,entry,side,base_contract,targets=None):
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
    target_id=targets['challenger' if side=='CHALLENGER' else 'champion'][entry['case_id']] if targets is not None else entry['case']['step_id']
    target=next((x for x in parts if x['step_id']==target_id),None)
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
        generation=one(c,'''SELECT g.generation AS started,s.generation AS current FROM text_experiment_generation g
            JOIN text_task_method_state s ON s.task_id=:task WHERE g.experiment_id=:id''',{'task':exp['task_id'],'id':experiment_id})
        latest_revision=one(c,'SELECT MAX(revision) AS revision FROM work_design_revisions WHERE design_id=:id',{'id':exp['design_id']})['revision']
        if (not generation or generation['started']!=generation['current'] or champion['version']!=exp['champion_version'] or digest(champion['output_schema'])!=exp['champion_contract_sha256']
            or suite['snapshot_sha256']!=exp['suite_sha256'] or digest(exp['candidate_plan'])!=exp['candidate_plan_sha256']
            or latest_revision!=exp['candidate_revision']):
            raise Conflict('champion, candidate or evaluation suite changed')
        targets=_targets(c,exp,snapshot)
        cases=[]
        for entry in snapshot['cases']:
            old=_side_result(c,exp,entry,'CHAMPION',champion['output_schema'],targets);new=_side_result(c,exp,entry,'CHALLENGER',champion['output_schema'],targets)
            cases.append({'case_id':entry['case_id'],'case_sha256':entry['case_sha256'],'step_id':entry['case']['step_id'],
                          'champion_step_id':targets['champion'][entry['case_id']],
                          'candidate_step_id':targets['challenger'][entry['case_id']],
                          'expected_behavior':entry['case']['expected_behavior'],'champion':old,'challenger':new})
        complete=all(x['champion']['valid_execution'] and x['challenger']['valid_execution'] for x in cases)
        regressions=[x['case_id'] for x in cases if x['champion']['checks_passed'] and not x['challenger']['checks_passed']]
        gains=[x['case_id'] for x in cases if not x['champion']['checks_passed'] and x['challenger']['checks_passed']]
        models={m for x in cases for side in ('champion','challenger') for m in x[side]['model_ids']}
        live=champion['output_schema']['model_mode']=='LIVE_LLM'
        comparable=complete and len(models)==1 and 'MOCK' not in models
        old_criteria=champion['output_schema']['plan']['evaluation_criteria']
        added=exp['candidate_plan']['evaluation_criteria'][len(old_criteria):]
        criteria_cases={criterion:[x['case_id'] for x in cases if x['expected_behavior']==criterion] for criterion in added}
        cases_by_id={x['case_id']:x for x in cases}
        added_pass=all(ids and all(cases_by_id[case_id]['challenger']['checks_passed'] for case_id in ids)
                       for ids in criteria_cases.values())
        old_expected=champion['output_schema']['plan']['expected_result']
        new_expected=exp['candidate_plan']['expected_result']
        expected_changed=old_expected!=new_expected
        expected_cases={value:[x['case_id'] for x in cases if x['expected_behavior']==value]
                        for value in (old_expected,new_expected)} if expected_changed else {}
        expected_pass=all(ids and all(cases_by_id[case_id]['challenger']['checks_passed'] for case_id in ids)
                          for ids in expected_cases.values())
        change_kind='EXPECTED_RESULT_CHANGE' if expected_changed else 'CRITERIA_ADDITION' if added else 'METHOD'
        gates={'all_executions_valid':complete,'same_live_model':live and comparable,'zero_regressions':not regressions,
               'at_least_one_gain':bool(gains),'all_cases_pass':all(x['challenger']['checks_passed'] for x in cases),
               'added_criteria_cases_pass':added_pass,'expected_result_cases_pass':expected_pass}
        result={'scope':'USER_DEFINED_SUBSTRING_CHECKS_NOT_SEMANTIC','suite_id':str(suite['suite_id']),
                'suite_sha256':suite['snapshot_sha256'],'champion_contract_sha256':exp['champion_contract_sha256'],
                'candidate_plan_sha256':exp['candidate_plan_sha256'],'champion_targets':targets['champion'],
                'candidate_targets':targets['challenger'],'target_mapping_sha256':digest(targets),
                'case_count':len(cases),'cases':cases,
                'champion_passed':sum(x['champion']['checks_passed'] for x in cases),
                'challenger_passed':sum(x['challenger']['checks_passed'] for x in cases),
                'gains':gains,'regressions':regressions,'gates':gates,'model_ids':sorted(models),
                'change_kind':change_kind,'added_criteria':added,'criterion_case_ids':criteria_cases,
                'expected_result_change':{'from':old_expected,'to':new_expected,'case_ids':expected_cases} if expected_changed else None,
                'semantic_quality':'NOT_EVALUATED','cost':'NOT_CALCULATED'}
        verdict='PASS' if evaluation_pass(result) else 'FAIL'
        c.execute(text('''INSERT INTO text_experiment_results(experiment_id,result,result_sha256,verdict)
            VALUES (:id,CAST(:result AS jsonb),:sha,:verdict)'''),{'id':experiment_id,'result':canonical(result),'sha':digest(result),'verdict':verdict})
        c.execute(text("UPDATE text_experiments SET status='EVALUATED' WHERE experiment_id=:id"),{'id':experiment_id})
        kernel._event(c,'text_experiment.evaluated',experiment_id,{'result_sha256':digest(result),'verdict':verdict,
            'suite_sha256':suite['snapshot_sha256'],'scope':result['scope']},actor='user',key=key)
        return True
