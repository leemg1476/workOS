import json
from types import SimpleNamespace
import openai
from fastapi.testclient import TestClient
from app.api.main import app
from app.worker import process_claim
from app.work.db import engine
from app.work.market import digest
from sqlalchemy import text


def install_model(monkeypatch, *, regress=False, vary_model=False):
    class Responses:
        def create(self, **kwargs):
            body=json.loads(kwargs['input'][1]['content'])
            purpose=body['step']['purpose']
            improved='개선' in purpose
            output=('배송 내용' if improved else '내용 누락')
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
