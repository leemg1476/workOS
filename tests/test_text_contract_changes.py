from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine
from app.worker import process_claim
from test_text_experiments import install_model, make_experiment, run_pair


def candidate(client, task, design, revision=3):
    context=client.get('/api/tasks/'+task+'/text-experiment-context').json()
    plan=context['plan']
    plan['objective']='고객 지연 원인과 다음 조치를 정리한다'
    plan['evaluation_criteria']=['다음 조치가 포함되는지 사람이 확인']
    assert client.put('/api/work-designs/'+design,json={'revision':revision,'plan':plan}).status_code==200
    return client.get('/api/tasks/'+task+'/text-experiment-context').json()


def request(context, key='apply'):
    return {'expected_version':context['version'],'candidate_revision':context['latest_revision'],
            'candidate_sha256':context['latest_plan_sha256'],'note':'운영 목표와 기준 변경 검토', 'idempotency_key':key}


def test_contract_change_is_explicit_versioned_and_reversible(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,experiment=make_experiment(client,kernel)
        exp=client.post('/api/tasks/'+task+'/text-experiments',json=experiment).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'finish'}).status_code==200
        before=client.get('/api/tasks/'+task+'/regression-suites').json()
        ctx=candidate(client,task,design)
        path='/api/tasks/'+task+'/text-contract-changes';body=request(ctx)
        assert client.post(path,json={**body,'candidate_sha256':'0'*64}).status_code==409
        response=client.post(path,json=body);assert response.status_code==200
        change=response.json()['change_id'];assert response.json()['created']
        assert client.post(path,json=body).json()=={'change_id':change,'created':False}
        assert client.post(path,json={**body,'note':'다른 요청'}).status_code==409
        assert client.get('/api/tasks/'+task+'/regression-suites').json()==before
        new=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        assert new['version']==2 and new['plan']['objective']=='고객 지연 원인과 다음 조치를 정리한다'
        assert new['plan']['evaluation_criteria']==['다음 조치가 포함되는지 사람이 확인']
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'오래된 비교','idempotency_key':'old'}).status_code==409
        work=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':'new-work','input_snapshot':{}}).json()['work_id']
        claim=kernel.claim();assert claim['work_id']==work
        result=process_claim(kernel,claim);assert result['passed'] and result['result']['requires_human_review']
        rollback={'expected_version':2,'note':'이전 목표 복구','idempotency_key':'rollback'}
        path='/api/text-contract-changes/'+change+'/rollback'
        assert client.post(path,json=rollback).json()['created']
        assert not client.post(path,json=rollback).json()['created']
        assert client.get('/api/tasks/'+task+'/text-experiment-context').json()['version']==1
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'원복 후에도 오래된 비교','idempotency_key':'old-after'}).status_code==409
        with engine.connect() as c:
            versions=c.execute(text('SELECT version,objective FROM task_definitions WHERE task_id=:task ORDER BY version'),{'task':task}).mappings().all()
            assert len(versions)==2 and versions[0]['objective']!=versions[1]['objective']
            assert c.execute(text('SELECT task_version FROM work_items WHERE work_id=:work'),{'work':work}).scalar_one()==2
            assert c.execute(text('SELECT task_version FROM job_definitions WHERE task_id=:task'),{'task':task}).scalar_one()==1
        history=client.get('/api/tasks/'+task+'/text-contract-changes').json()
        assert [x['decision'] for x in history]==['ROLLBACK','APPLY']


def test_contract_change_rejects_stale_or_unrelated_edits(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,_,_=make_experiment(client,kernel)
        ctx=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json=request(ctx)).status_code==409  # step change
        ctx=candidate(client,task,design)
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json={**request(ctx),'expected_version':99}).status_code==409
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json={**request(ctx),'note':'   '}).status_code==409
        plan=ctx['latest_plan'];plan['expected_result']='다른 기대 결과'
        assert client.put('/api/work-designs/'+design,json={'revision':ctx['latest_revision'],'plan':plan}).status_code==200
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json=request(ctx)).status_code==409
        ctx=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json=request(ctx)).status_code==409


def test_contract_change_preserves_applied_case_targets(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,suite,body=make_experiment(client,kernel)
        plan=client.get('/api/work-designs/'+design+'/revisions').json()[0]['plan']
        plan['steps'][0]['id']='answer'
        assert client.put('/api/work-designs/'+design,json={'revision':3,'plan':plan}).status_code==200
        case=client.get('/api/tasks/'+task+'/regression-cases').json()[0]['case_id']
        exp=client.post('/api/tasks/'+task+'/text-experiments',json={**body,'candidate_revision':4,'candidate_targets':{case:'answer'}}).json()['experiment_id']
        run_pair(kernel,1)
        assert client.post('/api/text-experiments/'+exp+'/finish',json={'idempotency_key':'mapping-finish'}).status_code==200
        assert client.post('/api/text-experiments/'+exp+'/decision',json={'decision':'APPLY','note':'대응 확인','idempotency_key':'mapping-apply'}).status_code==200
        ctx=candidate(client,task,design,revision=4)
        assert ctx['champion_targets']=={case:'answer'}
        assert client.post('/api/tasks/'+task+'/text-contract-changes',json=request(ctx)).status_code==200
        new=client.get('/api/tasks/'+task+'/text-experiment-context').json()
        assert new['champion_targets']=={case:'answer'}
        new_plan=new['plan'];new_plan['steps'][0]['purpose']='다음 방법 변경'
        assert client.put('/api/work-designs/'+design,json={'revision':5,'plan':new_plan}).status_code==200
        next_run=client.post('/api/tasks/'+task+'/text-experiments',json={'suite_id':suite,'candidate_revision':6,'idempotency_key':'after-contract'})
        assert next_run.status_code==200


def test_rollback_refuses_modified_previous_contract(kernel,monkeypatch):
    install_model(monkeypatch)
    with TestClient(app) as client:
        task,design,_,_=make_experiment(client,kernel)
        ctx=candidate(client,task,design)
        change=client.post('/api/tasks/'+task+'/text-contract-changes',json=request(ctx)).json()['change_id']
        with engine.begin() as c:
            c.execute(text('''UPDATE task_definitions SET output_schema=jsonb_set(output_schema,'{plan,objective}',
                to_jsonb(CAST('변조된 목표' AS text))) WHERE task_id=:task AND version=1'''),{'task':task})
        response=client.post('/api/text-contract-changes/'+change+'/rollback',json={
            'expected_version':2,'note':'이전 계약 복구','idempotency_key':'tampered-rollback'})
        assert response.status_code==409
        assert client.get('/api/tasks/'+task+'/text-experiment-context').json()['version']==2


def test_apply_serializes_with_design_revision(kernel,monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    install_model(monkeypatch)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        task,design,_,_=make_experiment(client,kernel)
        ctx=candidate(client,task,design)
        with engine.begin() as c:
            c.execute(text('SELECT design_id FROM work_designs WHERE design_id=:id FOR UPDATE'),{'id':design})
            pending=pool.submit(client.post,'/api/tasks/'+task+'/text-contract-changes',json=request(ctx))
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                with engine.connect() as observer:
                    waiting=observer.execute(text('''SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                        AND wait_event_type='Lock' AND query LIKE '%FROM work_designs WHERE design_id=%FOR UPDATE%' ''')).scalar_one()
                if waiting: break
                time.sleep(.05)
            assert waiting, 'apply did not wait for the design revision lock'
            c.execute(text('''INSERT INTO work_design_revisions(design_id,revision,description,plan,origin,model_id,response_id,feedback)
                SELECT design_id,revision+1,description,plan,origin,model_id,response_id,feedback
                FROM work_design_revisions WHERE design_id=:id AND revision=:revision'''),
                {'id':design,'revision':ctx['latest_revision']})
        assert pending.result(timeout=10).status_code==409
        assert client.get('/api/tasks/'+task+'/text-experiment-context').json()['version']==ctx['version']
