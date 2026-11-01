import json
from types import SimpleNamespace
import openai
from fastapi.testclient import TestClient
from app.api.main import app
from app.worker import process_claim
from app.work.db import engine
from app.work.market import digest
from sqlalchemy import text


def install_model(monkeypatch, *, regress=False, vary_model=False, always_pass=False):
    class Responses:
        def create(self, **kwargs):
            body=json.loads(kwargs['input'][1]['content'])
            purpose=body['step']['purpose']
            improved='개선' in purpose
            output=('배송 내용' if improved else '내용 누락')
            if always_pass:
                output='배송 내용'
            if regress and not improved and '두번째' in body['source_text']:
                output='배송 내용'
            if regress and improved and '두번째' in body['source_text']:
                output='내용 누락'
            model='different-model' if vary_model and improved else 'fixed-model'
            return SimpleNamespace(status='completed',output_text=output,model=model,id='synthetic-response',
                                   usage=SimpleNamespace(input_tokens=8,output_tokens=3))
    class Client:
        def __init__(self, **kwargs): self.responses=Responses()
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setenv('openai_api_key','test-only-key')
    monkeypatch.setenv('openai_model','fixed-model')


def make_experiment(client,kernel, *, second=False, mode='LIVE_LLM'):
    draft=client.post('/api/work-designs',json={'description':'배송 문의 정리','mode':'MOCK'}).json()
    plan=draft['plan'];plan['expected_result']='배송 문의 요약';plan['evaluation_criteria']=['배송 포함']
    plan['steps']=plan['steps'][:1];plan['steps'][0]['purpose']='기존 단계'
    assert client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan}).status_code==200
    registered=client.post('/api/work-designs/'+draft['design_id']+'/register',json={'revision':2,'source_text':'원본 배송 문의','model_mode':mode}).json()
    task=registered['task_id']
    work=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'baseline-work','input_snapshot':{}}).json()['work_id']
    claim=kernel.claim();result=process_claim(kernel,claim);assert result['passed']
    assert client.post('/api/work/'+work+'/review',json={'artifact_id':result['artifact_id'],'decision':'REJECT','note':'배송 누락','idempotency_key':'baseline-review'}).status_code==200
    cases=[]
    for n in range(2 if second else 1):
        request={'artifact_id':result['artifact_id'],'step_id':'collect','input_text':'익명 배송 문의 '+('두번째' if n else '첫번째'),
                 'expected_behavior':'배송을 언급','feedback':'배송 누락','required_phrases':['배송'],'forbidden_phrases':['환불'],
                 'idempotency_key':'case-'+str(n)}
        case=client.post('/api/tasks/'+task+'/regression-cases',json=request);assert case.status_code==200
        cases.append(case.json()['case_id'])
    suite=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':cases,'idempotency_key':'suite'}).json()['suite_id']
    plan['steps'][0]['purpose']='개선 단계: 배송을 구체적으로 확인'
    assert client.put('/api/work-designs/'+draft['design_id'],json={'revision':2,'plan':plan}).status_code==200
    request={'suite_id':suite,'candidate_revision':3,'idempotency_key':'experiment-start'}
    return task, draft['design_id'], suite, request


def run_pair(kernel,count):
    for _ in range(count*2):
        claim=kernel.claim();assert claim
        result=process_claim(kernel,claim);assert result and result['passed']


def test_paired_replay_same_fixed_input_and_gain(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,request=make_experiment(client,kernel)
        started=client.post('/api/tasks/'+task+'/text-experiments',json=request)
        assert started.status_code==200 and started.json()['created']
        exp=started.json()['experiment_id']
        assert client.post('/api/tasks/'+task+'/text-experiments',json=request).json()=={'experiment_id':exp,'created':False}
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'early'}).status_code==409
        run_pair(kernel,1)
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert len(info['runs'])==2 and {r['work_status'] for r in info['runs']}=={'WAITING_APPROVAL'}
        eval_artifact=client.get('/api/runs/'+info['runs'][0]['run_id']+'/artifacts').json()[0]
        assert client.post('/api/work/'+info['runs'][0]['work_id']+'/review',json={
            'artifact_id':eval_artifact['artifact_id'],'decision':'ACCEPT','note':'평가 산출물','idempotency_key':'must-not-accept'}).status_code==409
        assert len(client.get('/api/tasks/'+task+'/runs').json())==1
        with engine.connect() as c:
            pairs=c.execute(text('''SELECT m.side,ctx.manifest FROM text_experiment_work m
                JOIN runs r ON r.work_id=m.work_id JOIN run_contexts ctx ON ctx.run_id=r.run_id
                WHERE m.experiment_id=:id ORDER BY m.side'''),{'id':exp}).mappings().all()
            assert pairs[0]['manifest']['contract']['source_text']==pairs[1]['manifest']['contract']['source_text']=='익명 배송 문의 첫번째'
            assert {p['manifest']['evaluation']['side'] for p in pairs}=={'CHAMPION','CHALLENGER'}
        finalized=client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish'})
        assert finalized.status_code==200 and finalized.json()['changed']
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish'}).json()['changed'] is False
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert info['verdict']=='PASS' and info['result']['champion_passed']==0 and info['result']['challenger_passed']==1
        assert len(info['result']['gains'])==1 and not info['result']['regressions']
        assert info['result']['semantic_quality']=='NOT_EVALUATED' and info['result']['cost']=='NOT_CALCULATED'
        assert info['result']['cases'][0]['champion']['target_excerpt']=='내용 누락'
        assert info['result']['cases'][0]['challenger']['target_excerpt']=='배송 내용'
        with engine.connect() as c:
            active=c.execute(text('SELECT version FROM task_definitions WHERE task_id=:task AND active'),{'task':task}).scalar_one()
            assert active==1


def test_regression_blocks_even_when_another_case_improves(kernel,monkeypatch):
    install_model(monkeypatch,regress=True)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel,second=True)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,2)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-regression'}).status_code==200
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert info['verdict']=='FAIL' and len(info['result']['gains'])==1 and len(info['result']['regressions'])==1
        assert info['result']['gates']['zero_regressions'] is False


def test_model_mismatch_never_passes(kernel,monkeypatch):
    install_model(monkeypatch,vary_model=True)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-mismatch'}).status_code==200
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert info['verdict']=='FAIL' and not info['result']['gates']['same_live_model']


def test_mock_replay_is_not_eligible_as_live_improvement(kernel):
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel,mode='MOCK')
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-mock'}).status_code==200
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert info['verdict']=='FAIL' and not info['result']['gates']['same_live_model']
        assert info['result']['model_ids']==['MOCK']


def test_changed_candidate_or_new_suite_blocks_evaluation(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,1)
        revisions=client.get('/api/work-designs/'+design+'/revisions').json()
        plan=revisions[0]['plan'];plan['steps'][0]['purpose']='다시 변경'
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-stale'}).status_code==409
        assert client.get('/api/tasks/'+task+'/text-experiments').json()[0]['result'] is None


def test_new_suite_invalidates_prior_comparison(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,1)
        case_id=client.get('/api/tasks/'+task+'/regression-cases').json()[0]['case_id']
        assert client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':[case_id],'idempotency_key':'newer-suite'}).status_code==200
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-old-suite'}).status_code==409
        assert client.get('/api/tasks/'+task+'/text-experiments').json()[0]['result'] is None


def test_failing_worker_is_recorded_and_cannot_pass(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        monkeypatch.delenv('openai_api_key',raising=False)
        monkeypatch.delenv('OPENAI_API_KEY',raising=False)
        for _ in range(2):
            claim=kernel.claim();assert claim
            assert process_claim(kernel,claim) is None
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-failed'}).status_code==200
        info=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert info['verdict']=='FAIL' and not info['result']['gates']['all_executions_valid']
        assert all(r['run_status']=='FAILED' for r in info['runs'])
        assert client.post('/api/work/'+info['runs'][0]['work_id']+'/retry',json={'idempotency_key':'after-finish'}).status_code==409


def test_candidate_cannot_weaken_acceptance_criteria(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,_,request=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['evaluation_criteria']=['아무 결과나 허용']
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        response=client.post('/api/tasks/'+task+'/text-experiments',json={**request,'candidate_revision':4})
        assert response.status_code==409


def test_tampered_candidate_output_and_hash_cannot_be_evaluated(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        run_pair(kernel,1)
        with engine.begin() as c:
            artifact=c.execute(text('''SELECT a.artifact_id,a.content FROM text_experiment_work m
                JOIN runs r ON r.work_id=m.work_id JOIN artifacts a ON a.run_id=r.run_id
                WHERE m.experiment_id=:id AND m.side='CHALLENGER' '''),{'id':exp}).mappings().one()
            content=artifact['content'];content['steps'][0]['text']='조작된 배송 결과'
            c.execute(text('UPDATE artifacts SET content=CAST(:content AS jsonb),sha256=:sha WHERE artifact_id=:id'),
                      {'id':artifact['artifact_id'],'content':json.dumps(content,ensure_ascii=False),'sha':digest(content)})
        response=client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-tampered'})
        assert response.status_code==409
        assert client.get('/api/tasks/'+task+'/text-experiments').json()[0]['result'] is None


def test_changed_step_ids_require_explicit_case_target_and_are_applicable(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,request=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['steps'][0]['id']='answer'
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        case=client.get('/api/tasks/'+task+'/regression-cases').json()[0]['case_id']
        request={**request,'candidate_revision':4}
        assert client.post('/api/tasks/'+task+'/text-experiments',json=request).status_code==409
        assert client.post('/api/tasks/'+task+'/text-experiments',json={**request,'candidate_targets':{case:'missing'}}).status_code==409
        request['candidate_targets']={case:'answer'}
        start=client.post('/api/tasks/'+task+'/text-experiments',json=request)
        assert start.status_code==200
        exp=start.json()['experiment_id']
        assert client.post('/api/tasks/'+task+'/text-experiments',json={**request,'candidate_targets':{case:'collect'}}).status_code==409
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-new-target'}).status_code==200
        result=client.get('/api/tasks/'+task+'/text-experiments').json()[0]['result']
        assert result['candidate_targets']=={case:'answer'}
        assert result['cases'][0]['step_id']=='collect' and result['cases'][0]['candidate_step_id']=='answer'
        assert result['champion_passed']==0 and result['challenger_passed']==1
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'단계 대응을 검토','idempotency_key':'apply-new-target'}).status_code==200
        with engine.connect() as c:
            plan=c.execute(text('SELECT output_schema FROM task_definitions WHERE task_id=:task AND active'),{'task':task}).scalar_one()['plan']
            assert plan['steps'][0]['id']=='answer'
        context=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        assert context['champion_targets'][case]=='answer'
        plan['steps'][0]['id']='reply'
        assert client.put('/api/work-designs/'+design,json={'revision':4,'plan':plan}).status_code==200
        next_request={'suite_id':suite,'candidate_revision':5,'candidate_targets':{case:'reply'},'idempotency_key':'second-mapping'}
        second=client.post('/api/tasks/'+task+'/text-experiments',json=next_request)
        assert second.status_code==200
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+second.json()['experiment_id']+'/finish',json={'idempotency_key':'finish-second-mapping'}).status_code==200
        result=client.get('/api/tasks/'+task+'/text-experiments').json()[0]['result']
        assert result['champion_targets']=={case:'answer'} and result['candidate_targets']=={case:'reply'}
        assert result['cases'][0]['champion_step_id']=='answer' and result['cases'][0]['candidate_step_id']=='reply'


def test_case_target_mapping_is_immutable(kernel,monkeypatch):
    import pytest
    from sqlalchemy.exc import DBAPIError
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text("UPDATE text_experiment_targets SET targets='{}'::jsonb WHERE experiment_id=:id"),{'id':exp})


def test_new_suite_must_retain_previously_frozen_cases(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,_=make_experiment(client,kernel,second=True)
        cases=client.get('/api/tasks/'+task+'/regression-cases').json()
        response=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':[cases[0]['case_id']],'idempotency_key':'omit-prior'})
        assert response.status_code==409


def test_criteria_can_be_added_with_frozen_case_without_claiming_method_gain(kernel,monkeypatch):
    install_model(monkeypatch,always_pass=True)
    with TestClient(app) as client:
        task,design,suite,_=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['steps'][0]['purpose']='기존 단계'
        plan['evaluation_criteria'].append('사례가 없는 기준')
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        request={'suite_id':suite,'candidate_revision':4,'idempotency_key':'criteria-missing'}
        assert client.post('/api/tasks/'+task+'/text-experiments',json=request).status_code==409
        plan['evaluation_criteria'][-1]='배송을 언급'
        assert client.put('/api/work-designs/'+design,json={'revision':4,'plan':plan}).status_code==200
        response=client.post('/api/tasks/'+task+'/text-experiments',json={**request,'candidate_revision':5,'idempotency_key':'criteria-ok'})
        assert response.status_code==200
        exp=response.json()['experiment_id'];run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-criteria'}).status_code==200
        report=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert report['verdict']=='PASS' and report['result']['change_kind']=='CRITERIA_ADDITION'
        assert report['result']['gains']==[] and report['result']['gates']['all_cases_pass'] is True
        assert report['result']['semantic_quality']=='NOT_EVALUATED'
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'새 기준 검토','idempotency_key':'apply-criteria'}).status_code==200
        with engine.connect() as c:
            contract=c.execute(text('SELECT output_schema FROM task_definitions WHERE task_id=:task AND active'),{'task':task}).scalar_one()
            assert contract['plan']['evaluation_criteria']==['배송 포함','배송을 언급']


def test_criteria_and_workflow_cannot_change_in_same_experiment(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,_=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['evaluation_criteria'].append('배송을 언급')
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        response=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':suite,'candidate_revision':4,'idempotency_key':'two-changes'})
        assert response.status_code==409


def test_criteria_addition_requires_all_frozen_checks_to_pass(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,_=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['steps'][0]['purpose']='기존 단계'
        plan['evaluation_criteria'].append('배송을 언급')
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        response=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':suite,'candidate_revision':4,'idempotency_key':'criteria-fail'})
        exp=response.json()['experiment_id'];run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-criteria-fail'}).status_code==200
        report=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert report['verdict']=='FAIL' and not report['result']['gates']['all_cases_pass']
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'불통과','idempotency_key':'apply-failure'}).status_code==409


def test_expected_result_change_preserves_old_and_new_cases(kernel,monkeypatch):
    install_model(monkeypatch,always_pass=True)
    with TestClient(app) as client:
        task,design,suite,_=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['steps'][0]['purpose']='기존 단계';plan['expected_result']='배송과 다음 조치 요약'
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        request={'suite_id':suite,'candidate_revision':4,'idempotency_key':'missing-outcome-cases'}
        assert client.post('/api/tasks/'+task+'/text-experiments',json=request).status_code==409
        first=client.get('/api/tasks/'+task+'/regression-cases').json()[0]
        case_ids=[first['case_id']]
        for i,expected in enumerate(('배송 문의 요약','배송과 다음 조치 요약')):
            request_case={'artifact_id':first['snapshot']['source_artifact_id'],'step_id':'collect',
                'input_text':'익명 배송 문의','expected_behavior':expected,'feedback':'기대 결과 확인',
                'required_phrases':['배송'],'forbidden_phrases':[],'idempotency_key':f'expected-case-{i}'}
            response=client.post('/api/tasks/'+task+'/regression-cases',json=request_case)
            assert response.status_code==200;case_ids.append(response.json()['case_id'])
        new_suite=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':case_ids,'idempotency_key':'expected-suite'}).json()['suite_id']
        response=client.post('/api/tasks/'+task+'/text-experiments',json={**request,'suite_id':new_suite,'idempotency_key':'expected-ready'})
        assert response.status_code==200
        exp=response.json()['experiment_id'];run_pair(kernel,3)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-expected'}).status_code==200
        report=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert report['verdict']=='PASS' and report['result']['change_kind']=='EXPECTED_RESULT_CHANGE'
        assert report['result']['expected_result_change']['from']=='배송 문의 요약'
        assert report['result']['expected_result_change']['to']=='배송과 다음 조치 요약'
        assert len(report['result']['expected_result_change']['case_ids'])==2
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'두 기대 결과 사례 검토','idempotency_key':'apply-expected'}).status_code==200
        with engine.connect() as c:
            contract=c.execute(text('SELECT output_schema FROM task_definitions WHERE task_id=:task AND active'),{'task':task}).scalar_one()
            assert contract['plan']['expected_result']=='배송과 다음 조치 요약'


def test_paired_replay_handles_more_than_ten_retained_cases(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,_,_,request=make_experiment(client,kernel)
        original=client.get('/api/tasks/'+task+'/regression-cases').json()[0]
        ids=[original['case_id']]
        for i in range(10):
            body={'artifact_id':original['snapshot']['source_artifact_id'],'step_id':'collect',
                'input_text':f'익명 배송 문의 {i}','expected_behavior':f'배송 확인 {i}','feedback':'배송 확인',
                'required_phrases':['배송'],'forbidden_phrases':[],'idempotency_key':f'many-case-{i}'}
            response=client.post('/api/tasks/'+task+'/regression-cases',json=body)
            assert response.status_code==200;ids.append(response.json()['case_id'])
        suite=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':ids,'idempotency_key':'eleven-cases'}).json()['suite_id']
        exp=client.post('/api/tasks/'+task+'/text-experiments',json={**request,'suite_id':suite,'idempotency_key':'eleven-replay'}).json()['experiment_id']
        run_pair(kernel,11)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish-eleven'}).status_code==200
        report=client.get('/api/tasks/'+task+'/text-experiments').json()[0]
        assert report['result']['case_count']==11 and report['result']['challenger_passed']==11
        assert report['verdict']=='PASS'
