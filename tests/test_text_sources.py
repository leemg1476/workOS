from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.scheduler import tick
from app.worker import process_claim
from app.work.db import engine


def register(client,schedule=''):
    draft=client.post('/api/work-designs',json={'description':'문의 정리','mode':'MOCK'}).json()
    plan=draft['plan'];plan['expected_result']='문의 요약';plan['evaluation_criteria']=['주요 내용 확인']
    assert client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan}).status_code==200
    response=client.post('/api/work-designs/'+draft['design_id']+'/register',json={
        'revision':2,'source_text':'첫 번째 문의','model_mode':'MOCK','schedule':schedule})
    assert response.status_code==200
    return response.json()['task_id']


def test_manual_input_is_pinned_and_retries_do_not_pick_up_new_source(kernel):
    with TestClient(app) as client:
        task=register(client);path='/api/tasks/'+task+'/text-source'
        assert client.get(path).json()['version']==1
        old=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'old','input_snapshot':{}}).json()['work_id']
        change={'expected_version':1,'source_text':'두 번째 문의','idempotency_key':'update-source'}
        assert client.put(path,json=change).json()=={'task_id':task,'version':2,'changed':True}
        assert client.put(path,json=change).json()['changed'] is False
        assert client.put(path,json={**change,'source_text':'다른 입력'}).status_code==409
        assert client.put(path,json={'expected_version':1,'source_text':'오래된 수정','idempotency_key':'stale'}).status_code==409
        new=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'new','input_snapshot':{'source_text':'무시되는 외부 입력'}}).json()['work_id']
        for work,version,source in [(old,1,'첫 번째 문의'),(new,2,'두 번째 문의')]:
            claim=kernel.claim();assert str(claim['work_id'])==work
            result=process_claim(kernel,claim);assert result['passed']
            manifest=client.get('/api/runs/'+str(claim['run_id'])).json()['context']['manifest']
            assert manifest['source_version']==version and manifest['contract']['source_text']==source
            if work==old:
                assert client.post('/api/work/'+work+'/review',json={'artifact_id':result['artifact_id'],'decision':'REJECT',
                    'note':'재검토','idempotency_key':'old-review'}).status_code==200
                assert client.post('/api/work/'+work+'/retry',json={'idempotency_key':'old-retry'}).status_code==200
        retry=kernel.claim();assert str(retry['work_id'])==old
        assert process_claim(kernel,retry)['passed']
        manifest=client.get('/api/runs/'+str(retry['run_id'])).json()['context']['manifest']
        assert manifest['source_version']==1 and manifest['contract']['source_text']=='첫 번째 문의'


def test_scheduled_work_pins_input_at_trigger_time(kernel):
    with TestClient(app) as client:
        task=register(client,'0 9 * * 1');path='/api/tasks/'+task+'/text-source'
        with engine.begin() as c:
            c.execute(text("UPDATE job_definitions SET enabled=false WHERE job_id<>:job AND trigger_type='CRON'"),{'job':task+'-JOB'})
        assert client.put(path,json={'expected_version':1,'source_text':'예약 시점 문의','idempotency_key':'schedule-source'}).status_code==200
        now=datetime(2026,11,1,tzinfo=timezone.utc);tick(engine,now=now)
        with engine.connect() as c:
            due=c.execute(text('SELECT next_due_at FROM job_schedule_cursors WHERE job_id=:job'),{'job':task+'-JOB'}).scalar_one()
        assert tick(engine,now=due+timedelta(seconds=1))==1
        assert client.put(path,json={'expected_version':2,'source_text':'예약 직후 변경','idempotency_key':'after-schedule'}).status_code==200
        claim=kernel.claim();assert claim['task_id']==task
        assert process_claim(kernel,claim)['passed']
        manifest=client.get('/api/runs/'+str(claim['run_id'])).json()['context']['manifest']
        assert manifest['source_version']==2 and manifest['contract']['source_text']=='예약 시점 문의'


def test_concurrent_source_edits_do_not_overwrite_one_another(kernel):
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(app) as client:
        task=register(client);path='/api/tasks/'+task+'/text-source'
        def edit(n):
            return client.put(path,json={'expected_version':1,'source_text':f'새 입력 {n}','idempotency_key':f'edit-{n}'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(edit,[1,2]))==[200,409]
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM text_source_versions WHERE task_id=:task'),{'task':task}).scalar_one()==2
        import pytest
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text("UPDATE text_source_versions SET source_text='변조' WHERE task_id=:task AND version=1"),{'task':task})


def test_new_unpinned_work_fails_closed(kernel):
    from uuid import uuid4
    with TestClient(app) as client:
        task=register(client);work=str(uuid4())
        with engine.begin() as c:
            c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status)
                VALUES (:work,:job,:task,1,CAST('{}' AS jsonb),'READY')'''),{'work':work,'job':task+'-JOB','task':task})
            c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:work,'PENDING')"),{'work':work})
        claim=kernel.claim();assert str(claim['work_id'])==work
        assert process_claim(kernel,claim) is None
        with engine.connect() as c:
            row=c.execute(text('SELECT status,accepted_artifact_id FROM work_items WHERE work_id=:work'),{'work':work}).mappings().one()
            assert row['status']=='REWORK_REQUIRED' and row['accepted_artifact_id'] is None
