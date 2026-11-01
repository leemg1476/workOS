from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.worker import process_claim
from app.work.db import engine
from datetime import datetime, timezone, timedelta
from app.scheduler import tick


def test_register_run_human_review_retry_and_provenance(kernel):
    with TestClient(app) as client:
        draft=client.post('/api/work-designs',json={'description':'고객 문의를 요약','mode':'MOCK'}).json()
        path=f"/api/work-designs/{draft['design_id']}/register"
        settings={'revision':1,'source_text':'고객 문의: 배송이 지연됨','model_mode':'MOCK','schedule':''}
        assert client.post(path,json=settings).status_code==422
        draft['plan']['expected_result']='배송 문의 요약'
        draft['plan']['evaluation_criteria']=['배송 지연이 요약에 포함됐는지 확인']
        assert client.put(f"/api/work-designs/{draft['design_id']}",json={'revision':1,'plan':draft['plan']}).status_code==200
        settings['revision']=2
        first=client.post(path,json=settings)
        assert first.status_code==200 and first.json()['created']
        task=first.json()['task_id']
        assert client.post(path,json=settings).json()=={'task_id':task,'created':False}
        assert client.post(path,json={**settings,'source_text':'different'}).status_code==409
        started=client.post(f'/api/tasks/{task}/run',json={'idempotency_key':'human-work-1','input_snapshot':{}}).json()
        assert started['data_mode']=='USER_PROVIDED_TEXT'
        work=started['work_id']; claim=kernel.claim(); assert claim['work_id']==work
        result=process_claim(kernel,claim); assert result['passed'] and result['result']['requires_human_review']
        detail=client.get(f'/api/work/{work}').json()
        assert detail['work']['status']=='WAITING_APPROVAL' and detail['work']['accepted_artifact_id'] is None
        artifact=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()[0]
        assert artifact['kind']=='provided_text_report' and artifact['content']['semantic_review']=='NOT_PERFORMED'
        assert [x['step_id'] for x in artifact['content']['steps']]==['collect','produce','review']
        context=client.get(f"/api/runs/{claim['run_id']}").json()
        assert context['context']['manifest']['contract']['source_text']==settings['source_text']
        review={'artifact_id':artifact['artifact_id'],'decision':'REJECT','note':'결과 보완 필요','idempotency_key':'review-one'}
        assert client.post(f'/api/work/{work}/review',json=review).json()['recorded']
        assert client.post(f'/api/work/{work}/review',json=review).json()['recorded'] is False
        assert client.get(f'/api/work/{work}').json()['work']['status']=='REWORK_REQUIRED'
        assert client.post(f'/api/work/{work}/retry',json={'idempotency_key':'retry-1'}).status_code==200
        next_claim=kernel.claim(); assert next_claim['run_id']!=claim['run_id']
        next_result=process_claim(kernel,next_claim); assert next_result['passed']
        next_artifact=next_result['artifact_id']
        assert client.post(f'/api/work/{work}/review',json={**review,'idempotency_key':'old','decision':'ACCEPT'}).status_code==409
        review2={'artifact_id':next_artifact,'decision':'ACCEPT','note':'수동 검토 완료','idempotency_key':'review-two'}
        assert client.post(f'/api/work/{work}/review',json=review2).json()['recorded']
        detail=client.get(f'/api/work/{work}').json()
        assert detail['work']['status']=='COMMITTED' and detail['work']['accepted_artifact_id']==next_artifact
        hist=client.get(f'/api/tasks/{task}/runs').json()
        assert hist[0]['human_accepted'] is True and hist[1]['human_accepted'] is False
        assert any(e['event_type']=='work.reviewed' for e in detail['events'])


def test_register_revision_and_cron_are_guarded(kernel):
    with TestClient(app) as client:
        d=client.post('/api/work-designs',json={'description':'업무','mode':'MOCK'}).json()
        path=f"/api/work-designs/{d['design_id']}/register"
        base={'revision':1,'source_text':'고정 자료','model_mode':'MOCK','schedule':'0 9 * * 1'}
        assert client.post(path,json={**base,'schedule':'invalid'}).status_code==422
        d['plan']['expected_result']='사례 요약'
        d['plan']['evaluation_criteria']=['자료에 없는 사실이 없는지 확인']
        assert client.put(f"/api/work-designs/{d['design_id']}",json={'revision':1,'plan':d['plan']}).status_code==200
        assert client.post(path,json=base).status_code==409
        registration=client.post(path,json={**base,'revision':2}).json()
        with engine.connect() as c:
            row=c.execute(text('SELECT trigger_type,schedule FROM job_definitions WHERE task_id=:id'),{'id':registration['task_id']}).mappings().one()
            assert row['trigger_type']=='CRON' and row['schedule']=='0 9 * * 1'
        with engine.begin() as c:
            c.execute(text('UPDATE job_definitions SET enabled=false WHERE task_id<>:id'),{'id':registration['task_id']})
        start=datetime(2030,1,1,tzinfo=timezone.utc)
        tick(engine, now=start)
        with engine.connect() as c:
            due=c.execute(text('SELECT next_due_at FROM job_schedule_cursors WHERE job_id=:job'),{'job':registration['task_id']+'-JOB'}).scalar_one()
        tick(engine,now=due+timedelta(seconds=1))
        work=kernel.claim()
        assert work['task_id']==registration['task_id']
        outcome=process_claim(kernel,work)
        assert outcome['passed'] and outcome['result']['requires_human_review']
        with engine.connect() as c:
            assert c.execute(text('SELECT status FROM work_items WHERE work_id=:id'),{'id':work['work_id']}).scalar_one()=='WAITING_APPROVAL'
        schedule_path='/api/tasks/'+registration['task_id']+'/schedule'
        pause={'enabled':False,'idempotency_key':'pause-test'}
        assert client.post(schedule_path,json=pause).json()['changed'] is True
        assert client.post(schedule_path,json=pause).json()['changed'] is False
        assert client.post(schedule_path,json={'enabled':True,'idempotency_key':'pause-test'}).status_code==409
        assert client.post('/api/tasks/'+registration['task_id']+'/run',json={'idempotency_key':'paused-run','input_snapshot':{}}).status_code==404
        assert client.post(schedule_path,json={'enabled':True,'idempotency_key':'resume-test'}).json()['changed'] is True
        assert kernel.trigger(registration['task_id']+'-JOB','stale-schedule',{},expected_due=due)==(None,False)
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM job_schedule_cursors WHERE job_id=:job'),{'job':registration['task_id']+'-JOB'}).scalar_one()==0


def test_tampered_candidate_cannot_be_approved(kernel):
    with TestClient(app) as client:
        draft=client.post('/api/work-designs',json={'description':'사례 정리','mode':'MOCK'}).json()
        draft['plan']['expected_result']='사례 요약'
        draft['plan']['evaluation_criteria']=['입력과 일치하는지 확인']
        client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':draft['plan']})
        registration=client.post('/api/work-designs/'+draft['design_id']+'/register',json={'revision':2,'source_text':'가상 텍스트','model_mode':'MOCK'}).json()
        work=client.post('/api/tasks/'+registration['task_id']+'/run',json={'idempotency_key':'tampered-task','input_snapshot':{}}).json()['work_id']
        claim=kernel.claim();result=process_claim(kernel,claim)
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{steps,0,text}',CAST('\"변조됨\"' AS jsonb)) WHERE artifact_id=:id"),{'id':result['artifact_id']})
        review={'artifact_id':result['artifact_id'],'decision':'ACCEPT','note':'검토','idempotency_key':'tampered-review'}
        assert client.post('/api/work/'+work+'/review',json=review).status_code==409
        state=client.get('/api/work/'+work).json()['work']
        assert state['status']=='WAITING_APPROVAL' and state['accepted_artifact_id'] is None


def test_ten_distinct_designs_use_same_executor_and_review_gate(kernel):
    with TestClient(app) as client:
        ids=[]
        for n in range(10):
            draft=client.post('/api/work-designs',json={'description':f'업무 {n} 자료 요약','mode':'MOCK'}).json()
            draft['plan']['name']=f'업무 {n}'
            draft['plan']['expected_result']=f'업무 {n} 요약'
            draft['plan']['evaluation_criteria']=['입력과 요약의 일치 여부']
            draft['plan']['steps']=draft['plan']['steps'][:1+(n%3)]
            draft['plan']['steps'][0]['purpose']=f'가상 업무 {n}의 자료를 확인한다.'
            assert client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':draft['plan']}).status_code==200
            registered=client.post('/api/work-designs/'+draft['design_id']+'/register',json={
                'revision':2,'source_text':f'가상 자료 {n}','model_mode':'MOCK'}).json()
            ids.append(registered['task_id'])
            response=client.post('/api/tasks/'+registered['task_id']+'/run',json={'idempotency_key':f'ten-tasks-{n}','input_snapshot':{}})
            assert response.status_code==200
            claim=kernel.claim()
            assert claim['task_id']==registered['task_id']
            result=process_claim(kernel,claim)
            assert result['passed'] and result['result']['requires_human_review']
            assert client.get('/api/work/'+response.json()['work_id']).json()['work']['status']=='WAITING_APPROVAL'
        assert len(set(ids))==10
