from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine
from app.worker import process_claim


def prepared(client,kernel):
    draft=client.post('/api/work-designs',json={'description':'선행 결과를 정리','mode':'MOCK'}).json()
    plan=draft['plan'];plan['expected_result']='정리된 결과';plan['evaluation_criteria']=['입력을 확인']
    assert client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan}).status_code==200
    task=client.post('/api/work-designs/'+draft['design_id']+'/register',json={
        'revision':2,'source_text':'가상 자료','model_mode':'MOCK'}).json()['task_id']
    source,_=kernel.trigger('test-job','source',{})
    claim=kernel.claim();assert claim['work_id']==source
    assert kernel.submit(claim['run_id'],{'mode':'MOCK','task_id':'TEST','value':'v1'})['passed']
    target=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'target','input_snapshot':{}}).json()['work_id']
    kernel.add_dependency(source,target)
    claim=kernel.claim();assert claim['work_id']==target
    result=process_claim(kernel,claim);assert result['passed'] and result['result']['requires_human_review']
    return source,target,result['artifact_id']


def test_waiting_review_is_invalidated_and_retried_after_source_revalidates(kernel):
    with TestClient(app) as client:
        source,target,artifact=prepared(client,kernel)
        assert kernel.revalidate(source,'refresh-source')
        assert client.get('/api/work/'+target).json()['work']['status']=='STALE'
        body={'artifact_id':artifact,'decision':'ACCEPT','note':'이전 결과','idempotency_key':'stale-review'}
        assert client.post('/api/work/'+target+'/review',json=body).status_code==409
        assert client.post('/api/work/'+target+'/retry',json={'idempotency_key':'too-early'}).status_code==409
        run=kernel.claim();assert run['work_id']==source
        assert kernel.submit(run['run_id'],{'mode':'MOCK','task_id':'TEST','value':'v2'})['passed']
        assert client.post('/api/work/'+target+'/retry',json={'idempotency_key':'retry-after-source'}).status_code==200
        run=kernel.claim();assert run['work_id']==target
        new=process_claim(kernel,run);assert new['passed']
        body.update(artifact_id=new['artifact_id'],idempotency_key='fresh-review',note='새 근거 검토')
        assert client.post('/api/work/'+target+'/review',json=body).status_code==200
        detail=client.get('/api/work/'+target).json()
        assert detail['work']['status']=='COMMITTED' and len(detail['runs'])==2
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM artifacts WHERE run_id IN (SELECT run_id FROM runs WHERE work_id=:id)'),{'id':target}).scalar_one()==2


def test_review_fails_closed_if_dependency_is_no_longer_current(kernel):
    with TestClient(app) as client:
        source,target,artifact=prepared(client,kernel)
        # Simulate inconsistent storage to test the independent final acceptance gate.
        with engine.begin() as c:
            c.execute(text("UPDATE work_items SET status='STALE' WHERE work_id=:id"),{'id':source})
        response=client.post('/api/work/'+target+'/review',json={'artifact_id':artifact,'decision':'ACCEPT',
            'note':'검토','idempotency_key':'guard'})
        assert response.status_code==409
        with engine.connect() as c:
            assert c.execute(text('SELECT accepted_artifact_id FROM work_items WHERE work_id=:id'),{'id':target}).scalar_one() is None
            assert c.execute(text("SELECT count(*) FROM evidence WHERE artifact_id=:id AND verification_type='human_work_review'"),{'id':artifact}).scalar_one()==0
