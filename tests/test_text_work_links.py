from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.worker import process_claim
from app.work.db import engine


def task(client,name):
    d=client.post('/api/work-designs',json={'description':name,'mode':'MOCK'}).json()
    p=d['plan'];p['name']=name;p['expected_result']='결과';p['evaluation_criteria']=['근거 확인']
    assert client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':p}).status_code==200
    return client.post('/api/work-designs/'+d['design_id']+'/register',json={'revision':2,'source_text':name+' 입력','model_mode':'MOCK'}).json()['task_id']


def run_and_review(client,kernel,task_id,key):
    work=client.post('/api/tasks/'+task_id+'/run',json={'idempotency_key':key,'input_snapshot':{}}).json()['work_id']
    claim=kernel.claim();assert claim['work_id']==work
    result=process_claim(kernel,claim);assert result['passed']
    return work,result['artifact_id']


def accept(client,work,artifact,key):
    return client.post('/api/work/'+work+'/review',json={'artifact_id':artifact,'decision':'ACCEPT','note':'사람 검토', 'idempotency_key':key})


def test_accepted_source_creates_pinned_downstream_and_invalidates_old_work(kernel):
    with TestClient(app) as client:
        source=task(client,'선행 업무');target=task(client,'후속 업무')
        payload={'source_task_id':source,'target_task_id':target,'idempotency_key':'create-link'}
        link=client.post('/api/text-work-links',json=payload);assert link.status_code==200
        assert not client.post('/api/text-work-links',json=payload).json()['created']
        source_work,artifact=run_and_review(client,kernel,source,'source')
        assert client.get('/api/text-work-links').json()[0]['delivery_count']==0
        assert accept(client,source_work,artifact,'accept-source').status_code==200
        assert accept(client,source_work,artifact,'accept-source').json()['recorded'] is False
        info=client.get('/api/text-work-links').json()[0]
        assert info['delivery_count']==1;child=str(info['latest_work_id'])
        run=kernel.claim();assert run['work_id']==child
        result=process_claim(kernel,run);assert result['passed'] and result['result']['requires_human_review']
        report=client.get('/api/runs/'+run['run_id']+'/artifacts').json()[0]['content']
        evidence=client.get('/api/runs/'+run['run_id']+'/artifacts').json()[0]['evidence']
        assert any(e['verifier_version']=='provided-text-linked-structure-v1' for e in evidence)
        context=client.get('/api/runs/'+run['run_id']).json()['context']['manifest']['contract']
        assert report['data_mode']=='USER_AND_ACCEPTED_WORK'
        assert context['linked_source']['artifact_id']==artifact and context['linked_source']['work_id']==source_work
        assert context['linked_source']['content']['data_mode']=='USER_PROVIDED_TEXT'
        assert '선행 업무 결과' in context['source_text']
        assert client.get('/api/work/'+child).json()['work']['status']=='WAITING_APPROVAL'
        assert kernel.revalidate(source_work,'refresh-source')
        assert client.get('/api/work/'+child).json()['work']['status']=='STALE'
        assert accept(client,child,result['artifact_id'],'stale-child').status_code==409
        assert client.post('/api/work/'+child+'/retry',json={'idempotency_key':'retry-old'}).status_code==409
        run=kernel.claim();assert run['work_id']==source_work
        new=process_claim(kernel,run);assert new['passed']
        assert accept(client,source_work,new['artifact_id'],'accept-new').status_code==200
        links=client.get('/api/text-work-links').json();assert links[0]['delivery_count']==2
        newer=str(links[0]['latest_work_id']);assert newer!=child
        assert client.post('/api/work/'+child+'/retry',json={'idempotency_key':'retry-old-after'}).status_code==409
        run=kernel.claim();assert run['work_id']==newer
        result=process_claim(kernel,run);assert result['passed']
        assert accept(client,newer,result['artifact_id'],'accept-new-child').status_code==200


def test_link_cycle_and_disable_are_guarded(kernel):
    with TestClient(app) as client:
        a,b,c=(task(client,x) for x in ('A','B','C'))
        def link(x,y,key):return client.post('/api/text-work-links',json={'source_task_id':x,'target_task_id':y,'idempotency_key':key})
        assert link(a,a,'self').status_code==409
        ab=link(a,b,'ab').json()['link_id'];assert link(b,c,'bc').status_code==200
        assert link(c,a,'cycle').status_code==409
        state='/api/text-work-links/'+ab+'/state';body={'enabled':False,'expected_version':1,'idempotency_key':'disable'}
        assert client.post(state,json=body).json()['changed']
        assert not client.post(state,json=body).json()['changed']
        assert link(c,a,'cycle-after-disable').status_code==200
        assert client.post(state,json={'enabled':True,'expected_version':2,'idempotency_key':'enable-cycle'}).status_code==409
        work,artifact=run_and_review(client,kernel,a,'disabled-source')
        assert accept(client,work,artifact,'disabled-accept').status_code==200
        assert next(x for x in client.get('/api/text-work-links').json() if x['link_id']==ab)['delivery_count']==0


def test_linked_source_hash_is_rechecked_before_execution(kernel):
    with TestClient(app) as client:
        source=task(client,'입력');target=task(client,'출력')
        client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,'idempotency_key':'tamper-link'})
        work,artifact=run_and_review(client,kernel,source,'tamper-source')
        assert accept(client,work,artifact,'tamper-accept').status_code==200
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{model_mode}',to_jsonb(CAST('tampered' AS text))) WHERE artifact_id=:id"),{'id':artifact})
        run=kernel.claim();assert not process_claim(kernel,run)
        assert client.get('/api/work/'+run['work_id']).json()['work']['status']=='REWORK_REQUIRED'


def test_linked_source_hash_is_rechecked_at_human_acceptance(kernel):
    with TestClient(app) as client:
        source=task(client,'선행');target=task(client,'후속')
        client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,'idempotency_key':'review-link'})
        work,artifact=run_and_review(client,kernel,source,'review-source')
        assert accept(client,work,artifact,'review-source-accept').status_code==200
        run=kernel.claim();result=process_claim(kernel,run);assert result['passed']
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{model_mode}',to_jsonb(CAST('tampered' AS text))) WHERE artifact_id=:id"),{'id':artifact})
        assert accept(client,run['work_id'],result['artifact_id'],'review-target-accept').status_code==409
        assert client.get('/api/work/'+run['work_id']).json()['work']['status']=='WAITING_APPROVAL'


def test_failed_delivery_rolls_back_source_acceptance(kernel,monkeypatch):
    from app.workloads import text_sources
    from app.work.service import Conflict
    with TestClient(app) as client:
        source=task(client,'원천');target=task(client,'후속')
        client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,'idempotency_key':'atomic-link'})
        work,artifact=run_and_review(client,kernel,source,'atomic-source')
        original=text_sources.pin_source
        def fail(*args,**kwargs): raise Conflict('injected delivery failure')
        monkeypatch.setattr(text_sources,'pin_source',fail)
        assert accept(client,work,artifact,'atomic-accept').status_code==409
        detail=client.get('/api/work/'+work).json()
        assert detail['work']['status']=='WAITING_APPROVAL' and detail['work']['accepted_artifact_id'] is None
        assert client.get('/api/text-work-links').json()[0]['delivery_count']==0
        monkeypatch.setattr(text_sources,'pin_source',original)
        assert accept(client,work,artifact,'atomic-accept').status_code==200
        assert client.get('/api/text-work-links').json()[0]['delivery_count']==1


def test_chain_advances_only_after_each_human_acceptance(kernel):
    with TestClient(app) as client:
        a,b,c=(task(client,x) for x in ('first','second','third'))
        for source,target in ((a,b),(b,c)):
            assert client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,
                'idempotency_key':source+'-link'}).status_code==200
        first,artifact=run_and_review(client,kernel,a,'chain-first')
        assert accept(client,first,artifact,'chain-first-accept').status_code==200
        second=kernel.claim();result=process_claim(kernel,second);assert result['passed']
        links=client.get('/api/text-work-links').json()
        assert next(x for x in links if x['source_task_id']==b)['delivery_count']==0
        assert accept(client,second['work_id'],result['artifact_id'],'chain-second-accept').status_code==200
        third=kernel.claim();assert third['task_id']==c
        result=process_claim(kernel,third);assert result['passed']
        manifest=client.get('/api/runs/'+third['run_id']).json()['context']['manifest']
        assert manifest['contract']['linked_source']['work_id']==second['work_id']
        assert manifest['contract']['linked_source']['content']['data_mode']=='USER_AND_ACCEPTED_WORK'


def test_rejected_source_does_not_create_followup(kernel):
    with TestClient(app) as client:
        source=task(client,'source');target=task(client,'target')
        assert client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,'idempotency_key':'reject-link'}).status_code==200
        work,artifact=run_and_review(client,kernel,source,'reject-source')
        response=client.post('/api/work/'+work+'/review',json={'artifact_id':artifact,'decision':'REJECT',
            'note':'추가 확인 필요','idempotency_key':'reject-review'})
        assert response.status_code==200
        assert client.get('/api/text-work-links').json()[0]['delivery_count']==0
        assert kernel.claim() is None


def test_linked_work_recovers_expired_lease_without_duplicate_delivery(kernel):
    with TestClient(app) as client:
        source=task(client,'source');target=task(client,'target')
        client.post('/api/text-work-links',json={'source_task_id':source,'target_task_id':target,'idempotency_key':'lease-link'})
        work,artifact=run_and_review(client,kernel,source,'lease-source')
        assert accept(client,work,artifact,'lease-source-accept').status_code==200
        old=kernel.claim(lease_seconds=-1)
        current=kernel.claim()
        assert old['work_id']==current['work_id'] and old['run_id']!=current['run_id']
        result=process_claim(kernel,current);assert result['passed']
        runs=client.get('/api/work/'+current['work_id']).json()['runs']
        assert [run['status'] for run in runs]==['FAILED','SUCCEEDED']
        assert client.get('/api/text-work-links').json()[0]['delivery_count']==1
