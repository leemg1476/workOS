import json
from types import SimpleNamespace
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.api.main import app
from app.worker import process_claim
from app.work.task_memory import load, build
from app.domain.hashing import digest


def register(client, name='배송 문의', mode='MOCK'):
    draft=client.post('/api/work-designs',json={'description':name,'mode':'MOCK'}).json()
    plan=draft['plan'];plan['expected_result']='배송 문의 요약';plan['evaluation_criteria']=['배송 포함']
    client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan})
    return client.post('/api/work-designs/'+draft['design_id']+'/register',json={
        'revision':2,'source_text':'배송 지연 문의','model_mode':mode}).json()['task_id']


def trigger(client, task, key):
    response=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':key,'input_snapshot':{}})
    assert response.status_code==200
    return response.json()['work_id']


def test_memory_reuses_feedback_in_model_and_pins_history(kernel, monkeypatch):
    calls=[]
    class Responses:
        def create(self, **kwargs):
            calls.append(json.loads(kwargs['input'][1]['content']))
            return SimpleNamespace(status='completed',output_text='배송 문의 요약',model='test-model',id='test-response',usage=None)
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=Responses()))
    monkeypatch.setenv('openai_api_key','test-key');monkeypatch.setenv('openai_model','test-model')
    with TestClient(app) as client:
        task=register(client,mode='LIVE_LLM');work=trigger(client,task,'first')
        first=kernel.claim();result=process_claim(kernel,first);assert result['passed']
        assert not calls[0]['task_memory']['reviews']
        review={'artifact_id':result['artifact_id'],'decision':'REJECT','note':'지연 원인을 명시하고 추측은 구분','idempotency_key':'reject'}
        assert client.post('/api/work/'+work+'/review',json=review).status_code==200
        before=client.get('/api/tasks/'+task+'/memory').json()
        assert before['recent_work'][0]['next_action']=='RETRY'
        assert before['memory']['accepted_results']==[]
        assert client.post('/api/work/'+work+'/retry',json={'idempotency_key':'retry'}).status_code==200
        second=kernel.claim();assert second['work_id']==work and second['run_id']!=first['run_id']
        result=process_claim(kernel,second);assert result['passed']
        assert calls[-1]['task_memory']['reviews'][0]['note']==review['note']
        frozen=client.get('/api/runs/'+second['run_id']).json()['task_memory']
        assert digest(frozen['manifest'])==frozen['sha256']
        assert client.post('/api/work/'+work+'/review',json={**review,'artifact_id':result['artifact_id'],
            'decision':'ACCEPT','note':'採用','idempotency_key':'accept'}).status_code==200
        assert client.get('/api/runs/'+second['run_id']).json()['task_memory']==frozen
        trigger(client,task,'third');third=kernel.claim();assert process_claim(kernel,third)['passed']
        memory=calls[-1]['task_memory'];assert memory['accepted_results'][0]['artifact_id']==result['artifact_id']
        assert memory['accepted_results'][0]['semantic_review']=='NOT_PERFORMED'
        with kernel.engine.connect() as c:
            assert load(c,first['run_id'])['manifest']['reviews']==[]
        with pytest.raises(DBAPIError), kernel.engine.begin() as c:
            c.execute(text('UPDATE run_task_memories SET sha256=:sha WHERE run_id=:id'),{'sha':'changed','id':first['run_id']})


def test_memory_failure_isolation_stale_and_version_boundaries(kernel):
    with TestClient(app) as client:
        task=register(client);other=register(client,'다른 업무')
        work=trigger(client,task,'failure');first=kernel.claim();kernel.fail(first['run_id'],'synthetic provider timeout')
        assert client.get('/api/tasks/'+task+'/memory').json()['memory']['failures'][0]['reason']=='synthetic provider timeout'
        trigger(client,other,'other');other_run=kernel.claim();assert process_claim(kernel,other_run)['passed']
        assert client.get('/api/runs/'+other_run['run_id']).json()['task_memory']['manifest']['failures']==[]
        kernel.retry(work,'retry-failure');second=kernel.claim();result=process_claim(kernel,second);assert result['passed']
        assert client.post('/api/work/'+work+'/review',json={'artifact_id':result['artifact_id'],'decision':'ACCEPT','note':'검토 완료','idempotency_key':'accept'}).status_code==200
        assert len(client.get('/api/tasks/'+task+'/memory').json()['memory']['accepted_results'])==1
        kernel.invalidate(work,'source changed')
        assert client.get('/api/tasks/'+task+'/memory').json()['memory']['accepted_results']==[]
        with kernel.engine.connect() as c:
            assert build(c,task,2)['failures']==[]
            isolated=build(c,task,1,evaluation=True)
            assert isolated['evaluation_isolated'] and isolated['failures']==[] and isolated['reviews']==[]
        assert client.get('/api/tasks/nonexistent/memory').status_code==404


def test_fixed_evaluation_runs_never_receive_operational_memory(kernel, monkeypatch):
    from test_text_experiments import install_model, make_experiment, run_pair
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,request=make_experiment(client,kernel)
        assert client.get('/api/tasks/'+task+'/memory').json()['memory']['reviews']
        response=client.post('/api/tasks/'+task+'/text-experiments',json=request)
        assert response.status_code==200
        run_pair(kernel,1)
        with kernel.engine.connect() as c:
            memories=c.execute(text('''SELECT m.manifest FROM run_task_memories m JOIN runs r USING(run_id)
                JOIN text_experiment_work x ON x.work_id=r.work_id''')).scalars().all()
        assert len(memories)==2
        assert memories[0]==memories[1]
        assert memories[0]['evaluation_isolated'] and memories[0]['reviews']==[]


def test_transient_execution_retries_same_work_with_budget_and_memory(kernel, monkeypatch):
    from app.workloads.registry import kind_executors
    def timeout(*args):
        raise TimeoutError('provider details must not be stored')
    monkeypatch.setitem(kind_executors,'provided_text_workflow_v1',timeout)
    with TestClient(app) as client:
        task=register(client);work=trigger(client,task,'transient')
        runs=[]
        for attempt in range(1,4):
            claim=kernel.claim();assert claim['work_id']==work and claim['attempt_no']==attempt
            runs.append(claim['run_id']);process_claim(kernel,claim)
            detail=client.get('/api/work/'+work).json()
            assert detail['runs'][-1]['error']=='workload execution failed: TimeoutError'
            assert detail['work']['status']==('READY' if attempt<3 else 'REWORK_REQUIRED')
            assert len(client.get('/api/runs/'+claim['run_id']).json()['task_memory']['manifest']['failures'])==attempt-1
            if attempt<3:
                assert kernel.claim() is None  # Respect retry backoff.
                with kernel.engine.begin() as c:
                    c.execute(text('UPDATE work_queue SET available_at=now() WHERE work_id=:id'),{'id':work})
        assert len(set(runs))==3 and kernel.claim() is None
        assert sum(e['event_type']=='run.retry_scheduled' for e in detail['events'])==2
