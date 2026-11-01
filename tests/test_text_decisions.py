from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine
from test_text_experiments import install_model, make_experiment, run_pair


def ready(client,kernel,monkeypatch):
    install_model(monkeypatch)
    task,design,suite,request=make_experiment(client,kernel)
    exp=client.post('/api/tasks/'+task+'/text-experiments',json=request).json()['experiment_id']
    run_pair(kernel,1)
    assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish'}).status_code==200
    return task,design,suite,exp


def test_apply_and_rollback_keep_job_and_block_old_evaluation(kernel,monkeypatch):
    with TestClient(app) as client:
        task,_,_,exp=ready(client,kernel,monkeypatch)
        from app.worker import process_claim
        prior_work=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'prior-work','input_snapshot':{}}).json()['work_id']
        prior_result=process_claim(kernel,kernel.claim())
        body={'decision':'APPLY','note':'비교 결과를 확인함','idempotency_key':'apply'}
        assert client.post('/api/text-experiments/'+exp+'/decision',json=body).json()['changed']
        assert not client.post('/api/text-experiments/'+exp+'/decision',json=body).json()['changed']
        with engine.connect() as c:
            job=c.execute(text('SELECT job_id,task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).mappings().one()
            assert job['job_id']==task+'-JOB' and job['task_version']==2
        assert client.post('/api/work/'+prior_work+'/review',json={'artifact_id':prior_result['artifact_id'],
            'decision':'ACCEPT','note':'이전 버전 결과','idempotency_key':'old-review'}).status_code==409
        new_work=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'post-apply','input_snapshot':{}}).json()['work_id']
        with engine.connect() as c:
            assert c.execute(text('SELECT task_version FROM work_items WHERE work_id=:id'),{'id':new_work}).scalar_one()==2
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'ROLLBACK','note':'원복','idempotency_key':'rollback'}).status_code==200
        with engine.connect() as c:
            assert c.execute(text('SELECT task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).scalar_one()==1
            assert c.execute(text('SELECT version FROM task_definitions WHERE task_id=:task AND active'),{'task':task}).scalar_one()==1
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'재적용','idempotency_key':'again'}).status_code==409
        assert {x['decision'] for x in client.get('/api/tasks/'+task+'/text-experiments').json()[0]['decisions']}=={'APPLY','ROLLBACK'}


def test_reject_does_not_change_active_task(kernel,monkeypatch):
    with TestClient(app) as client:
        task,_,_,exp=ready(client,kernel,monkeypatch)
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'REJECT','note':'추가 확인 필요','idempotency_key':'reject'}).status_code==200
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'적용','idempotency_key':'apply'}).status_code==409
        with engine.connect() as c:
            assert c.execute(text('SELECT task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).scalar_one()==1


def test_new_suite_blocks_apply(kernel,monkeypatch):
    with TestClient(app) as client:
        task,_,_,exp=ready(client,kernel,monkeypatch)
        case=client.get('/api/tasks/'+task+'/regression-cases').json()[0]['case_id']
        assert client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':[case],'idempotency_key':'new-suite'}).status_code==200
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'적용','idempotency_key':'apply'}).status_code==409


def test_rollback_does_not_revalidate_an_older_comparison(kernel,monkeypatch):
    with TestClient(app) as client:
        task,_,suite,first=ready(client,kernel,monkeypatch)
        second=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':suite,'candidate_revision':3,'idempotency_key':'second'}).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+second+'/finish',json={'idempotency_key':'finish-second'}).status_code==200
        assert client.post('/api/text-experiments/'+first+'/decision',json={'decision':'APPLY','note':'승인','idempotency_key':'apply-first'}).status_code==200
        assert client.post('/api/text-experiments/'+first+'/decision',json={'decision':'ROLLBACK','note':'원복','idempotency_key':'roll-first'}).status_code==200
        assert client.post('/api/text-experiments/'+second+'/decision',json={'decision':'APPLY','note':'오래된 비교','idempotency_key':'apply-second'}).status_code==409


def test_apply_rechecks_evidence_and_preserves_current_version(kernel,monkeypatch):
    import json
    from app.work.market import digest
    with TestClient(app) as client:
        task,_,_,exp=ready(client,kernel,monkeypatch)
        with engine.begin() as c:
            artifact=c.execute(text('''SELECT a.artifact_id,a.content FROM text_experiment_work m JOIN runs r ON r.work_id=m.work_id
                JOIN artifacts a ON a.run_id=r.run_id WHERE m.experiment_id=:id AND m.side='CHALLENGER' '''),{'id':exp}).mappings().one()
            content=artifact['content'];content['steps'][0]['text']='변조 배송 결과'
            c.execute(text('UPDATE artifacts SET content=CAST(:content AS jsonb),sha256=:sha WHERE artifact_id=:id'),
                      {'id':artifact['artifact_id'],'content':json.dumps(content,ensure_ascii=False),'sha':digest(content)})
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'적용','idempotency_key':'apply'}).status_code==409
        with engine.connect() as c:
            assert c.execute(text('SELECT task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).scalar_one()==1


def test_reevaluation_can_reapply_same_registered_revision_after_rollback(kernel,monkeypatch):
    with TestClient(app) as client:
        task,design,suite,first=ready(client,kernel,monkeypatch)
        assert client.post('/api/text-experiments/'+first+'/decision',json={'decision':'APPLY','note':'적용','idempotency_key':'first-apply'}).status_code==200
        assert client.post('/api/text-experiments/'+first+'/decision',json={'decision':'ROLLBACK','note':'복구','idempotency_key':'first-rollback'}).status_code==200
        registration=client.post('/api/work-designs/'+design+'/register',json={'revision':3,'source_text':'원본 배송 문의','model_mode':'LIVE_LLM'})
        assert registration.status_code==409
        second=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':suite,'candidate_revision':3,'idempotency_key':'retry-evaluation'}).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+second+'/finish',json={'idempotency_key':'finish-again'}).status_code==200
        assert client.post('/api/text-experiments/'+second+'/decision',json={'decision':'APPLY','note':'재평가','idempotency_key':'apply-again'}).status_code==200
        with engine.connect() as c:
            assert c.execute(text('SELECT task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).scalar_one()==2
