from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine
from app.worker import process_claim
from test_text_work_links import task, run_and_review, accept


def setup_join(client):
    a,b,target=(task(client,x) for x in ('source-a','source-b','target'))
    body={'source_task_ids':[a,b],'target_task_id':target,'idempotency_key':'join-create'}
    response=client.post('/api/text-work-joins',json=body);assert response.status_code==200,response.text
    join=response.json()['join_id']
    assert not client.post('/api/text-work-joins',json=body).json()['created']
    return a,b,target,join


def reviewed(client,kernel,task_id,key):
    work,artifact=run_and_review(client,kernel,task_id,key)
    response=accept(client,work,artifact,key+'-accept');assert response.status_code==200,response.text
    return work,artifact


def group(client,join):return next(x for x in client.get('/api/text-work-joins').json() if x['join_id']==join)


def test_join_waits_for_all_and_uses_latest_new_result_once(kernel):
    with TestClient(app) as client:
        a,b,target,join=setup_join(client)
        old,old_art=reviewed(client,kernel,a,'a1')
        first=group(client,join);assert first['firing_count']==0
        assert {x['source_task_id']:x['state'] for x in first['inputs']}=={a:'READY',b:'WAITING'}
        newer,new_art=reviewed(client,kernel,a,'a2')
        assert group(client,join)['firing_count']==0
        work_b,art_b=reviewed(client,kernel,b,'b1')
        g=group(client,join);assert g['firing_count']==1
        assert {x['state'] for x in g['inputs']}=={'WAITING'}
        claim=kernel.claim();assert claim['work_id']==g['latest_work_id'] and claim['task_id']==target
        out=process_claim(kernel,claim);assert out['passed'] and out['result']['requires_human_review']
        run=client.get('/api/runs/'+claim['run_id']).json();bundle=run['context']['manifest']['contract']['joined_sources']
        assert {x['artifact_id'] for x in bundle['sources']}=={new_art,art_b}
        assert old_art not in {x['artifact_id'] for x in bundle['sources']}
        evidence=client.get('/api/runs/'+claim['run_id']+'/artifacts').json()[0]['evidence']
        assert any(x['verifier_version']=='provided-text-joined-structure-v4' for x in evidence)
        assert accept(client,claim['work_id'],out['artifact_id'],'joined-accept').status_code==200
        reviewed(client,kernel,b,'b2')
        assert group(client,join)['firing_count']==1
        reviewed(client,kernel,a,'a3')
        assert group(client,join)['firing_count']==2


def test_join_stale_pending_and_existing_output(kernel):
    with TestClient(app) as client:
        a,b,_,join=setup_join(client)
        source_a,_=reviewed(client,kernel,a,'a-one')
        assert kernel.revalidate(source_a,'stale-pending')
        run=kernel.claim();assert run['work_id']==source_a
        result=process_claim(kernel,run);assert result['passed']
        reviewed(client,kernel,b,'b-one')
        assert group(client,join)['firing_count']==0
        assert next(x['state'] for x in group(client,join)['inputs'] if x['source_task_id']==a)=='STALE'
        assert accept(client,source_a,result['artifact_id'],'fresh-a').status_code==200
        g=group(client,join);assert g['firing_count']==1
        run=kernel.claim();assert run['work_id']==g['latest_work_id']
        target=process_claim(kernel,run);assert target['passed']
        assert kernel.revalidate(source_a,'invalidate-combination')
        assert client.get('/api/work/'+run['work_id']).json()['work']['status']=='STALE'
        assert accept(client,run['work_id'],target['artifact_id'],'invalid-target').status_code==409
        assert client.post('/api/work/'+run['work_id']+'/retry',json={'idempotency_key':'old-join-retry'}).status_code==409


def test_join_cycle_and_restart_discards_pending(kernel):
    with TestClient(app) as client:
        a,b,target,join=setup_join(client)
        assert client.post('/api/text-work-links',json={'source_task_id':target,'target_task_id':a,'idempotency_key':'back-edge'}).status_code==409
        reviewed(client,kernel,a,'pending-a')
        path='/api/text-work-joins/'+join+'/state'
        assert client.post(path,json={'enabled':False,'expected_version':1,'idempotency_key':'stop'}).status_code==200
        assert client.post(path,json={'enabled':True,'expected_version':2,'idempotency_key':'restart'}).status_code==200
        reviewed(client,kernel,b,'after-restart-b')
        assert group(client,join)['firing_count']==0
        assert {x['source_task_id']:x['state'] for x in group(client,join)['inputs']}=={a:'WAITING',b:'READY'}
        reviewed(client,kernel,a,'after-restart-a')
        assert group(client,join)['firing_count']==1


def test_join_tampered_source_blocks_execution(kernel):
    with TestClient(app) as client:
        a,b,_,join=setup_join(client)
        _,artifact=reviewed(client,kernel,a,'tamper-a')
        reviewed(client,kernel,b,'tamper-b')
        work=group(client,join)['latest_work_id']
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{model_mode}',to_jsonb(CAST('tampered' AS text))) WHERE artifact_id=:id"),{'id':artifact})
        claim=kernel.claim();assert claim['work_id']==work
        assert not process_claim(kernel,claim)
        assert client.get('/api/work/'+work).json()['work']['status']=='REWORK_REQUIRED'
