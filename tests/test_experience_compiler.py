import json
from copy import deepcopy
from types import SimpleNamespace
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.api.main import app
from app.domain.hashing import digest
from app.work.memory_compiler import compile_step
from app.worker import process_claim
from test_task_knowledge import register,run,propose,SOURCE,EXPRESSION


def install_writer(monkeypatch):
    state={'omit_components':False,'requests':[]}
    class Responses:
        def create(self,**kwargs):
            body=json.loads(kwargs['input'][1]['content']);state['requests'].append(body)
            active=body['task_memory'].get('knowledge',{}).get('active',[])
            rules=[r for r in active if r['kind']=='CHECKLIST']
            value='테스트 기준 보고서. 실측 가계 자료가 아닙니다.'
            if rules or any(r['kind']=='CLAIM_CHECK' for r in active):
                source=json.loads(body['source_text']);intro='입력에 근거한 가상 계산입니다.';claims=[]
                if any(r['kind']=='CLAIM_CHECK' for r in active):
                    intro=f"{source['month']} 식품 상승률은 {source['food_rate']}% 상승입니다."
                    claims=[{'text':intro,'source_path':'/food_rate','value':source['food_rate'],'direction':'UP','period_path':'/month','period_value':source['month']}]
                value=json.dumps({'introduction':intro,'sections':[{'rule_id':r['entry_id'],'text':'계산의 입력과 의미를 설명합니다.',
                    'components':{} if state['omit_components'] else {k:v for k,v in r.get('component_spec',{}).items() if k in ('formula','input_values','interpretation')}} for r in rules],'claims':claims},ensure_ascii=False)
            return SimpleNamespace(status='completed',output_text=value,model='fixture-writer',id='fixture-response',output=[],usage=SimpleNamespace(input_tokens=100,output_tokens=50))
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:SimpleNamespace(responses=Responses()))
    monkeypatch.setenv('openai_api_key','fixture-key');monkeypatch.setenv('openai_model','fixture-writer')
    return state


def setup_task(client,kernel,unit='KRW'):
    task=register(client,mode='LIVE_LLM')
    source={**SOURCE,'month':'2025-09'}
    assert client.put('/api/tasks/'+task+'/text-source',json={'source_text':json.dumps(source),'expected_version':1,'idempotency_key':task+'-source'}).status_code==200
    binding={'family':'household_report','contract_key':'fixed-basket-v1','step_roles':{'report':'WRITE'},
        'input_fields':{p:{'type':'number','unit':unit,'meaning':meaning} for p,unit,meaning in [
            ('/food','KRW','식품 기준지출'),('/other','KRW','비식품 기준지출'),('/food_rate','percent','식품 상승률'),('/other_rate','percent','비식품 상승률')]}}
    binding['input_fields']['/month']={'type':'string','unit':'month','meaning':'보고기간'}
    binding['input_fields']['/food']['unit']=unit;binding['input_fields']['/other']['unit']=unit
    response=client.post('/api/tasks/'+task+'/procedure-binding',json={'binding':binding,'idempotency_key':task+'-binding'})
    assert response.status_code==200,response.text
    return task,response.json()['binding_sha256'],source


def rule_spec(binding,key='formula-explanation',value='REQUIRE'):
    return {'rule_key':key,'failure_mode':'OUTPUT_OMISSION','root_cause':'계산 결과만 제시해 입력·산식·해석을 검토할 수 없었다.',
        'scope':{'kind':'TASK_CONTRACT','binding_sha256':binding},'target_steps':['report'],'target_roles':[],
        'when':[{'path':'/source/food','op':'EXISTS'}],'unless':[],
        'actions':[{'kind':'REQUIRE_SECTION','action_id':'formula','heading':'계산 방법과 한계','instruction':'입력값·산식·가상 예시라는 의미를 설명한다.',
            'required_components':['formula','input_values','interpretation'],'formula':EXPRESSION,
            'input_paths':['/food','/other','/food_rate','/other_rate'],'interpretation_code':'FIXED_BASKET_ESTIMATE'}],
        'verification':{'suite_key':'household-regression','severity':'BLOCK','semantic_review':'DOMAIN_ASSERTIONS'},
        'policy_slot':'formula-explanation','policy_value':value}


def candidate(client,task,binding,key='formula-explanation',value='REQUIRE'):
    legacy=propose(client,task,'LESSON',{'text':'산식 설명을 누락하지 않는다.'},key+'-original')
    response=client.post('/api/knowledge/'+legacy['entry_id']+'/normalize',json={'rule':rule_spec(binding,key,value),'title':'산식 설명을 실행·검사 가능한 규칙으로 전환',
        'note':'테스트 fixture의 정규화 검토이며 실제 사람 품질평가가 아님','idempotency_key':key+'-normalize'})
    assert response.status_code==200,response.text
    entry=next(e for e in client.get('/api/tasks/'+task+'/knowledge').json() if e['entry_id']==response.json()['entry_id'])
    return legacy,entry


def compare(client,kernel,task,entry,source,*,expect_valid=True):
    origin,result=run(client,kernel,task,entry['entry_id']+'-origin');assert result['passed']
    assert client.post('/api/work/'+origin['work_id']+'/review',json={'artifact_id':result['artifact_id'],'decision':'REJECT','note':'테스트 fixture: 산식 설명 누락','idempotency_key':entry['entry_id']+'-review'}).status_code==200
    cases=[]
    for n in range(2):
        data={**source,'month':'2025-10' if n else '2025-09','food':500000 if n else 800000}
        assertions=[{'suite_key':'household-regression','kind':'FORMULA_EXPLANATION','formula':EXPRESSION,
                'input_paths':['/food','/other','/food_rate','/other_rate'],'interpretation_code':'FIXED_BASKET_ESTIMATE'}]
        if any(a['kind']=='CHECK_CLAIMS' for a in entry['payload']['actions']):
            assertions.append({'suite_key':'household-regression','kind':'SUPPORTED_CLAIMS','source_paths':['/food_rate'],'require_period':True,'period_path':'/month'})
        response=client.post('/api/tasks/'+task+'/regression-cases',json={'artifact_id':result['artifact_id'],'step_id':'report',
            'input_text':json.dumps(data),'expected_behavior':'고정수량 산식·입력·해석','feedback':'누락 재발 방지',
            'domain_assertions':assertions,
            'split':'HELDOUT' if n else 'REGRESSION','idempotency_key':entry['entry_id']+f'-case{n}'})
        assert response.status_code==200,response.text;cases.append(response.json()['case_id'])
    prior=client.get('/api/tasks/'+task+'/regression-suites').json()
    if prior:cases=list(dict.fromkeys(cases+[c['case_id'] for c in prior[0]['snapshot']['cases']]))
    response=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':cases,'idempotency_key':entry['entry_id']+'-suite'})
    assert response.status_code==200,response.text
    response=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':response.json()['suite_id'],'candidate_revision':2,
        'knowledge_candidate_id':entry['entry_id'],'idempotency_key':entry['entry_id']+'-comparison'})
    assert response.status_code==200,response.text
    experiment=response.json()['experiment_id']
    for _ in range(len(cases)*2):
        claim=kernel.claim();assert claim;result=process_claim(kernel,claim)
        assert result and (not expect_valid or result['passed'])
    response=client.post('/api/text-experiments/'+experiment+'/finish',json={'idempotency_key':experiment+'-finish'})
    assert response.status_code==200,response.text
    info=next(x for x in client.get('/api/tasks/'+task+'/text-experiments').json() if x['experiment_id']==experiment)
    return experiment,info


def activate(client,entry,experiment,key='activate',**extra):
    return client.post('/api/knowledge/'+entry['entry_id']+'/decision',json={'decision':'APPLY','expected_sha256':entry['sha256'],
        'evaluation_id':experiment,'note':'격리 테스트 사용자 Apply 시뮬레이션','idempotency_key':key,**extra})


def test_normalization_preserves_original_and_rejects_incomplete_contract(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);old,new=candidate(client,task,binding)
        rows=client.get('/api/tasks/'+task+'/knowledge').json()
        assert next(r for r in rows if r['entry_id']==old['entry_id'])['payload']==old['payload']
        assert new['kind']=='RULE' and new['relations'][0]['kind']=='DERIVED_FROM'
        assert activate(client,new,None).status_code==409
        invalid=rule_spec(binding);invalid['actions'][0].pop('formula')
        assert client.post('/api/tasks/'+task+'/knowledge',json={'kind':'RULE','title':'불완전 후보','payload':invalid,'idempotency_key':'bad-rule'}).status_code==422
        changed=client.get('/api/tasks/'+task+'/procedure-binding').json()['binding'];changed['input_fields']['/food']['unit']='USD'
        assert client.post('/api/tasks/'+task+'/procedure-binding',json={'binding':changed,'idempotency_key':'changed-unit'}).status_code==409


def test_paired_rule_evaluation_transfer_and_application_trace(kernel,monkeypatch):
    state=install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);old,new=candidate(client,task,binding)
        experiment,info=compare(client,kernel,task,new,source)
        assert info['verdict']=='PASS' and info['result']['challenger_passed']==2 and info['result']['champion_passed']==0
        assert info['result']['gates']['knowledge_heldout_coverage']
        assert activate(client,new,experiment).status_code==200
        data={**source,'month':'2025-10','food':600000,'food_rate':4.2}
        assert client.put('/api/tasks/'+task+'/text-source',json={'source_text':json.dumps(data),'expected_version':2,'idempotency_key':'october'}).status_code==200
        claim,result=run(client,kernel,task,'october-report');assert result['passed']
        report=client.get('/api/runs/'+claim['run_id']+'/artifacts').json()[0]
        assert '600000' in report['content']['steps'][0]['text'] and '산식:' in report['content']['steps'][0]['text']
        traces=client.get('/api/runs/'+claim['run_id']+'/memory-application').json()
        selection=next(t['snapshot'] for t in traces if t['phase']=='SELECT')
        verification=next(t['snapshot'] for t in traces if t['phase']=='VERIFY')
        assert selection['selected_rule_ids']==[new['entry_id']]
        assert verification['passed'] and verification['results'][0]['checks']=={'section_present':True,'formula':True,'input_values':True,'interpretation':True}
        # A nonempty section with arbitrary text no longer satisfies the rule.
        state['omit_components']=True
        failed,result=run(client,kernel,task,'omitted-formula');assert not result['passed']
        assert client.get('/api/runs/'+failed['run_id']+'/artifacts').json()
        assert not result['result']['checks']['compiled_rule_compliance']
        with pytest.raises(DBAPIError),kernel.engine.begin() as c:
            c.execute(text("UPDATE memory_application_traces SET step_id='tamper' WHERE run_id=:id"),{'id':claim['run_id']})


def test_conflicting_policy_does_not_activate(kernel,monkeypatch):
    state=install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);_,new=candidate(client,task,binding)
        experiment,info=compare(client,kernel,task,new,source)
        assert activate(client,new,experiment).status_code==200
        _,conflict=candidate(client,task,binding,'opposite','FORBID')
        denied=activate(client,conflict,None,key='conflict')
        assert denied.status_code==409 and 'contradicting' in denied.text
        assert next(e for e in client.get('/api/tasks/'+task+'/knowledge').json() if e['entry_id']==conflict['entry_id'])['decision']=='PROPOSED'


def test_failed_knowledge_evaluation_cannot_be_called_improved(kernel,monkeypatch):
    state=install_writer(monkeypatch);state['omit_components']=True
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);_,entry=candidate(client,task,binding)
        experiment,info=compare(client,kernel,task,entry,source,expect_valid=False)
        assert info['verdict']=='FAIL' and not info['result']['gates']['all_executions_valid']
        assert activate(client,entry,experiment).status_code==409
        assert client.post('/api/text-experiments/'+experiment+'/decision',json={'decision':'APPLY','note':'불가','idempotency_key':'wrong-route'}).status_code==409


def test_family_procedure_reuse_excludes_changed_field_units(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel)
        spec=rule_spec(binding);spec['scope']['kind']='TASK_FAMILY'
        entry=propose(client,task,'RULE',spec,'family-rule')
        experiment,_=compare(client,kernel,task,entry,source)
        assert activate(client,entry,experiment).status_code==200
        other,_,_=setup_task(client,kernel)
        claim,result=run(client,kernel,other,'same-family');assert result['passed']
        memory=client.get('/api/runs/'+claim['run_id']).json()['task_memory']['manifest']
        assert memory['knowledge']['active'][0]['entry_id']==entry['entry_id']
        usd,_,_=setup_task(client,kernel,unit='USD')
        claim,result=run(client,kernel,usd,'other-unit');assert result['passed']
        memory=client.get('/api/runs/'+claim['run_id']).json()['task_memory']['manifest']
        assert not memory['knowledge']['active'] and memory['knowledge']['excluded'][0]['reason']=='INPUT_FIELD_SEMANTICS_CHANGED'


def test_supersedes_atomic_replacement_and_rollback_keep_history(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);_,first=candidate(client,task,binding)
        old_exp,_=compare(client,kernel,task,first,source)
        assert activate(client,first,old_exp).status_code==200
        spec=rule_spec(binding,'formula-with-claims');spec['actions'].append({'kind':'CHECK_CLAIMS','action_id':'facts','source_paths':['/food_rate'],'require_period':True,'period_path':'/month'})
        second=propose(client,task,'RULE',spec,'formula-and-facts')
        assert client.post('/api/knowledge/'+second['entry_id']+'/relations',json={'kind':'SUPERSEDES','target_entry_id':first['entry_id'],'note':'교정·보강한 새 절차','idempotency_key':'supersedes'}).status_code==200
        new_exp,info=compare(client,kernel,task,second,source)
        assert info['verdict']=='PASS'
        assert activate(client,second,new_exp,key='replace',replace_entries={first['entry_id']:first['sha256']}).status_code==200
        rows=client.get('/api/tasks/'+task+'/knowledge').json();decisions={r['entry_id']:r['decision'] for r in rows}
        assert decisions[first['entry_id']]=='RETIRE' and decisions[second['entry_id']]=='APPLY'
        # An older suite is stale for new activation; rollback must be explicitly
        # revalidated against the latest suite, not silently trust a historic score.
        denied=activate(client,first,old_exp,key='rollback-stale',replace_entries={second['entry_id']:second['sha256']})
        assert denied.status_code==409
        assert 'newer evaluation suite' in denied.text
        restored=client.post('/api/knowledge/'+first['entry_id']+'/decision',json={'decision':'ROLLBACK','expected_sha256':first['sha256'],
            'evaluation_id':old_exp,'replace_entries':{second['entry_id']:second['sha256']},'note':'이전 승인 규칙으로 명시적 복구. 최신suite 재통과 주장이 아님.',
            'idempotency_key':'explicit-rollback'})
        assert restored.status_code==200,restored.text
        rows=client.get('/api/tasks/'+task+'/knowledge').json();decisions={r['entry_id']:r['decision'] for r in rows}
        assert decisions[first['entry_id']]=='APPLY' and decisions[second['entry_id']]=='RETIRE'


def test_step_selection_exclusion_and_precondition_are_explicit():
    spec=rule_spec('a'*64);spec['target_steps']=['report'];spec['unless']=[{'path':'/source/skip','op':'EQ','value':True}]
    memory={'knowledge':{'active':[{'entry_id':'r1','kind':'RULE','title':'규칙','sha256':'b'*64,'payload':spec}]},'reviews':[]}
    binding={'step_roles':{'research':'RESEARCH','report':'WRITE'},'input_fields':{}}
    contract={'source_text':json.dumps(SOURCE),'plan':{'steps':[{'id':'research'},{'id':'report'}]}}
    selected,trace=compile_step(memory,contract,'research',binding)
    assert not trace['selected_rule_ids'] and trace['excluded_reasons'][0]['reason']=='TARGET_STEP_MISMATCH'
    contract['source_text']=json.dumps({**SOURCE,'skip':True})
    selected,trace=compile_step(memory,contract,'report',binding)
    assert trace['excluded_reasons'][0]['reason']=='EXCLUSION_CONDITION'
    spec['unless']=[];spec['actions']=[{'kind':'PRECONDITION','action_id':'collect-first','conditions':[{'path':'/source/food','op':'EXISTS'}],'required_steps':['research']}]
    contract['source_text']=json.dumps(SOURCE)
    _,missing=compile_step(memory,contract,'report',binding)
    _,done=compile_step(memory,contract,'report',binding,completed_steps=['research'])
    assert not missing['preconditions'][0]['passed'] and done['preconditions'][0]['passed']


def test_human_review_gate_does_not_substitute_automatic_scores(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel)
        spec=rule_spec(binding);spec['verification']['semantic_review']='HUMAN_REQUIRED'
        entry=propose(client,task,'RULE',spec,'human-judgment-rule')
        experiment,info=compare(client,kernel,task,entry,source)
        assert info['verdict']=='PASS'
        assert activate(client,entry,experiment).status_code==409
        packet=client.get('/api/text-experiments/'+experiment+'/semantic-review').json()
        assert set(packet['cases'][0]['outputs'])=={'A','B'} and 'champion' not in packet['cases'][0]['outputs']
        from app.evaluation.knowledge_semantics import label_mapping
        judgments=[]
        for case in packet['cases']:
            labels=label_mapping(experiment,case['case_id'])
            judgments.append({'case_id':case['case_id'],'preference':next(k for k,v in labels.items() if v=='champion'),'note':'fixture 사용자 기각 판단 시뮬레이션'})
        rejected=client.post('/api/text-experiments/'+experiment+'/semantic-review',json={'packet_sha256':packet['packet_sha256'],
            'judgments':judgments,'note':'fixture only; 실제 사람 평가 결과 아님','idempotency_key':'human-reject'})
        assert rejected.status_code==200 and activate(client,entry,experiment,key='after-rejection').status_code==409
        for j in judgments:j['preference']='TIE'
        passed=client.post('/api/text-experiments/'+experiment+'/semantic-review',json={'packet_sha256':packet['packet_sha256'],
            'judgments':judgments,'note':'fixture 사용자 동등 판단 시뮬레이션','idempotency_key':'human-pass'})
        assert passed.status_code==200 and activate(client,entry,experiment,key='after-human-judgment').status_code==200


def test_metrics_need_independent_recall_labels_and_count_failed_compliance(kernel,monkeypatch):
    state=install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);_,entry=candidate(client,task,binding)
        experiment,_=compare(client,kernel,task,entry,source)
        assert activate(client,entry,experiment).status_code==200
        claim,result=run(client,kernel,task,'observed-use');assert result['passed']
        before=client.get('/api/tasks/'+task+'/knowledge-metrics').json()
        assert before['metrics']['applicable_recall']['value'] is None
        assert before['rules'][entry['entry_id']]['selected']==1  # Replay executions are a separate utility population.
        label=client.post('/api/runs/'+claim['run_id']+'/applicability-label',json={'step_id':'report','expected_rule_ids':[entry['entry_id']],
            'note':'테스트 fixture의 독립 적용 대상 라벨','idempotency_key':'recall-label'})
        assert label.status_code==200,label.text
        state['omit_components']=True
        _,result=run(client,kernel,task,'failed-use');assert not result['passed']
        after=client.get('/api/tasks/'+task+'/knowledge-metrics').json()
        assert after['metrics']['applicable_recall']['value']==1
        assert after['metrics']['rule_compliance']['value']==.5
        assert after['rules'][entry['entry_id']]['failed']==1
        assert after['rules'][entry['entry_id']]['knowledge_utility'][0]['quality_delta']['paired_case_gains']==2


def test_compatible_method_version_inherits_approved_procedure_binding(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel);_,entry=candidate(client,task,binding)
        experiment,_=compare(client,kernel,task,entry,source)
        assert activate(client,entry,experiment).status_code==200
        context=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        plan=deepcopy(context['plan']);plan['name']+=' · 표시 이름 정리'
        assert client.put('/api/work-designs/'+context['design_id'],json={'revision':2,'plan':plan}).status_code==200
        changed=client.post('/api/tasks/'+task+'/text-contract-changes',json={'expected_version':1,'candidate_revision':3,'candidate_sha256':digest(plan),
            'note':'테스트 fixture의 명시적 표시 계약 변경','idempotency_key':'compatible-version'})
        assert changed.status_code==200,changed.text
        assert client.get('/api/tasks/'+task+'/procedure-binding').json()['task_version']==2
        claim,result=run(client,kernel,task,'version-two');assert result['passed']
        memory=client.get('/api/runs/'+claim['run_id']).json()['task_memory']['manifest']
        assert memory['task_version']==2 and memory['knowledge']['active'][0]['entry_id']==entry['entry_id']


def test_claim_checks_reject_wrong_direction_and_period():
    from app.work.memory_compiler import verify_output
    spec=rule_spec('a'*64);spec['actions']=[{'kind':'CHECK_CLAIMS','action_id':'facts','source_paths':['/food_rate'],'require_period':True,'period_path':'/month'}]
    source={**SOURCE,'month':'2025-09','food_rate':-3.2}
    memory={'knowledge':{'active':[{'entry_id':'r1','kind':'RULE','title':'기간과 방향','sha256':'b'*64,'payload':spec}]},'reviews':[]}
    contract={'source_text':json.dumps(source),'plan':{'steps':[{'id':'report'}]}}
    selected,_=compile_step(memory,contract,'report',{'step_roles':{'report':'WRITE'}})
    bad={'introduction':'2025-10 식품이 3.2% 상승했다.','sections':[],'claims':[{'text':'2025-10 식품이 3.2% 상승했다.',
        'source_path':'/food_rate','value':-3.2,'direction':'DOWN','period_path':'/month','period_value':'2025-10'}]}
    assert not verify_output(json.dumps(bad),selected['knowledge'],contract['source_text'])[0]['passed']
    bad['introduction']='2025-09 식품이 3.2% 하락했다.';bad['claims'][0].update(text=bad['introduction'],period_value='2025-09')
    assert verify_output(json.dumps(bad),selected['knowledge'],contract['source_text'])[0]['passed']


def test_experience_analysis_uses_work_run_and_only_proposes_rules(kernel,monkeypatch):
    install_writer(monkeypatch)
    with TestClient(app) as client:
        task,binding,source=setup_task(client,kernel)
        episode,result=run(client,kernel,task,'experience-episode');assert result['passed']
        feedback=client.post('/api/work/'+episode['work_id']+'/agent-feedback',json={'artifact_id':result['artifact_id'],
            'note':'산식 설명 누락을 조사할 필요가 있습니다. 아직 사실 확정이 아닙니다.','evaluator_model':'fixture-reviewer','rubric_version':'fixture-v1','idempotency_key':'episode-feedback'})
        assert feedback.status_code==200
        obs=client.get('/api/tasks/'+task+'/experience-observations').json()[0]
        assert obs['classification']=='OUTPUT_OMISSION' and obs['snapshot']['classification_scope']=='UNVERIFIED_ROUTING_HINT'
        request={'mode':'MOCK','expected_binding_sha256':binding,'idempotency_key':'compile-fixture'}
        proposed=client.post('/api/experience-observations/'+obs['observation_id']+'/compile',json=request)
        assert proposed.status_code==200,proposed.text
        repeated=client.post('/api/experience-observations/'+obs['observation_id']+'/compile',json=request)
        assert repeated.json()['work_id']==proposed.json()['work_id'] and not repeated.json()['created']
        claim=kernel.claim();assert claim['work_id']==proposed.json()['work_id']
        result=process_claim(kernel,claim);assert result and result['passed']
        entries=client.get('/api/tasks/'+task+'/knowledge').json()
        rules=[e for e in entries if e['kind']=='RULE'];assert len(rules)==1 and rules[0]['decision']=='PROPOSED'
        artifact=client.get('/api/runs/'+claim['run_id']+'/artifacts').json()[0]
        assert artifact['content']['model_mode']=='MOCK' and artifact['content']['scope']=='UNVERIFIED_RULE_PROPOSAL'
        assert client.get('/api/tasks/'+task+'/experience-observations').json()[0]['compilations'][0]['run_status']=='SUCCEEDED'
