from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.evaluation.poc_voc_fixtures import PERIODS, cases
from app.work.db import engine
from app.worker import process_claim


def gold(period):
    return {x['record_id']:x['expected_category'] for x in cases(period)}


def baseline(client,seconds=3600):
    body={'answers':gold('week1'),'human_seconds':seconds,'idempotency_key':'baseline'}
    response=client.post('/api/poc-voc/baseline',json=body)
    assert response.status_code==200,response.text
    assert not client.post('/api/poc-voc/baseline',json=body).json()['created']
    return response.json()['measurement_id']


def run(client,kernel,period,key):
    response=client.post('/api/tasks/TASK-003/run',json={'idempotency_key':key,'input_snapshot':{'source_version':PERIODS[period]}})
    assert response.status_code==200,response.text
    claim=kernel.claim();assert claim['work_id']==response.json()['work_id']
    outcome=process_claim(kernel,claim);assert outcome and outcome['passed']
    view=client.get('/api/poc-voc/runs/'+claim['run_id'],params={'period':period})
    assert view.status_code==200,view.text
    return claim,view.json()


def test_human_baseline_and_verified_work_run_are_separate_measures(kernel):
    with TestClient(app) as client:
        rows=client.get('/api/poc-voc/inputs/week1').json()['records']
        assert len(rows)==24 and all('expected_category' not in x for x in rows)
        base=baseline(client)
        claim,view=run(client,kernel,'week1','run-week1')
        assert view['verifier_version']=='voc-poc-validator-v1' and view['suite_version']=='voc-poc-run-suite-v1'
        answers=gold('week1')
        changed=[key for key in answers if answers[key]!=view['predictions'][key]]
        payload={'baseline_id':base,'run_id':claim['run_id'],'period':'week1','answers':answers,
                 'reviewed_ids':changed,'human_seconds':420,'idempotency_key':'assisted1'}
        saved=client.post('/api/poc-voc/assisted',json=payload);assert saved.status_code==200,saved.text
        assert not client.post('/api/poc-voc/assisted',json=payload).json()['created']
        assert client.post('/api/poc-voc/assisted',json={**payload,'human_seconds':421}).status_code==409
        rows=client.get('/api/poc-voc/measurements').json();old,new=rows
        assert old['kind']=='BASELINE' and old['human_seconds']==3600 and old['time_basis']=='SELF_REPORTED'
        assert old['snapshot']['data_mode']=='SYNTHETIC_FIXTURE' and old['snapshot']['result']['correct']==24
        assert new['snapshot']['result']['correct']==24 and new['snapshot']['raw_result']['correct']<24
        assert new['snapshot']['reviewed_count']==len(changed) and new['snapshot']['changed_ids']==sorted(changed)
        assert new['snapshot']['run']['artifact_id']==view['artifact_id']
        assert new['snapshot']['run']['method_version']==view['method_version']
        with engine.begin() as c:
            try:
                c.execute(text('UPDATE poc_voc_measurements SET human_seconds=1 WHERE measurement_id=:id'),{'id':base})
                raise AssertionError('measurement must be immutable')
            except Exception as exc:
                assert 'immutable' in str(exc).lower() or 'append-only' in str(exc).lower()


def test_second_week_is_distinct_and_changed_answers_require_review(kernel):
    with TestClient(app) as client:
        base=baseline(client,300)
        claim,view=run(client,kernel,'week2','run-week2')
        answers=gold('week2');changed=[x for x in answers if answers[x]!=view['predictions'][x]]
        payload={'baseline_id':base,'run_id':claim['run_id'],'period':'week2','answers':answers,
                 'reviewed_ids':[],'human_seconds':60,'idempotency_key':'next-week'}
        assert changed and client.post('/api/poc-voc/assisted',json=payload).status_code==409
        response=client.post('/api/poc-voc/assisted',json={**payload,'reviewed_ids':changed})
        assert response.status_code==200,response.text
        base_row,next_row=client.get('/api/poc-voc/measurements').json()
        assert base_row['snapshot']['fixture_sha256']!=next_row['snapshot']['fixture_sha256']
        assert next_row['period']=='week2' and next_row['time_basis']=='SELF_REPORTED'
        assert (next_row['created_at'][:10]==base_row['created_at'][:10])


def test_inputs_and_evidence_cannot_be_substituted(kernel):
    with TestClient(app) as client:
        base=baseline(client)
        claim,view=run(client,kernel,'week1','tamper-run')
        assert client.get('/api/poc-voc/runs/'+claim['run_id'],params={'period':'week2'}).status_code==409
        assert client.post('/api/poc-voc/baseline',json={'answers':{},'human_seconds':3600,'idempotency_key':'bad'}).status_code==422
        assert client.post('/api/poc-voc/baseline',json={'answers':gold('week1'),'human_seconds':True,'idempotency_key':'bool'}).status_code==422
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{model_mode}',to_jsonb(CAST('tampered' AS text))) WHERE artifact_id=:id"),{'id':view['artifact_id']})
        payload={'baseline_id':base,'run_id':claim['run_id'],'period':'week1','answers':gold('week1'),
                 'reviewed_ids':list(gold('week1')),'human_seconds':100,'idempotency_key':'tampered'}
        assert client.post('/api/poc-voc/assisted',json=payload).status_code==409


def test_recorded_baseline_must_precede_run(kernel):
    with TestClient(app) as client:
        claim,view=run(client,kernel,'week1','too-early')
        base=baseline(client)
        payload={'baseline_id':base,'run_id':claim['run_id'],'period':'week1','answers':view['predictions'],
                 'reviewed_ids':[],'human_seconds':90,'idempotency_key':'order'}
        assert client.post('/api/poc-voc/assisted',json=payload).status_code==409


def test_opted_in_weekly_job_is_not_due_early_and_pins_fixture(kernel):
    from datetime import datetime, timedelta
    from app.scheduler import tick
    with TestClient(app) as client:
        base=baseline(client)
        baseline_row=client.get('/api/poc-voc/measurements').json()[0]
        request={'baseline_id':base,'idempotency_key':'weekly-opt-in'}
        response=client.post('/api/poc-voc/schedules',json=request);assert response.status_code==200,response.text
        schedule_id=response.json()['schedule_id']
        assert not client.post('/api/poc-voc/schedules',json=request).json()['created']
        assert client.post('/api/poc-voc/schedules',json={**request,'idempotency_key':'duplicate'}).status_code==409
        schedules=client.get('/api/poc-voc/schedules').json();s=schedules[0]
        due=datetime.fromisoformat(s['next_due_at']);base_at=datetime.fromisoformat(baseline_row['created_at'])
        assert due>=base_at+timedelta(days=7)
        assert len([x for x in client.get('/api/tasks').json() if x['task_id']=='TASK-003'])==1
        state='/api/poc-voc/schedules/'+schedule_id+'/state'
        assert client.post(state,json={'enabled':False,'idempotency_key':'stop'}).json()['changed']
        assert client.post(state,json={'enabled':True,'idempotency_key':'resume'}).json()['changed']
        resumed=client.get('/api/poc-voc/schedules').json()[0]
        assert datetime.fromisoformat(resumed['next_due_at'])>=base_at+timedelta(days=7)
        with engine.begin() as c:
            c.execute(text("UPDATE job_definitions SET enabled=false WHERE job_id<>:job"),{'job':s['job_id']})
        assert tick(engine,now=due-timedelta(seconds=1))==0
        assert tick(engine,now=due+timedelta(seconds=1))==1
        assert tick(engine,now=due+timedelta(seconds=1))==0
        claim=kernel.claim();assert claim['task_id']=='TASK-003'
        assert claim['input_snapshot']['source_version']==PERIODS['week2']
        assert claim['input_snapshot']['poc_baseline_id']==base and claim['input_snapshot']['scheduled_for']
        result=process_claim(kernel,claim);assert result and result['passed']
        view=client.get('/api/poc-voc/runs/'+claim['run_id'],params={'period':'week2'}).json()
        assert view['scheduled_baseline_id']==base and view['scheduled_for']
        latest=client.get('/api/poc-voc/schedules').json()[0]
        assert latest['latest_work_id']==claim['work_id'] and latest['latest_run_status']=='SUCCEEDED'


def test_scheduled_run_cannot_be_attributed_to_a_different_baseline(kernel):
    from datetime import datetime, timedelta
    from app.scheduler import tick
    with TestClient(app) as client:
        first=baseline(client)
        other=client.post('/api/poc-voc/baseline',json={'answers':gold('week1'),'human_seconds':800,'idempotency_key':'second-baseline'}).json()['measurement_id']
        client.post('/api/poc-voc/schedules',json={'baseline_id':first,'idempotency_key':'first-schedule'})
        schedule=client.get('/api/poc-voc/schedules').json()[0]
        due=datetime.fromisoformat(schedule['next_due_at'])
        with engine.begin() as c:
            c.execute(text("UPDATE job_definitions SET enabled=false WHERE job_id<>:job"),{'job':schedule['job_id']})
        assert tick(engine,now=due+timedelta(seconds=1))==1
        claim=kernel.claim();assert process_claim(kernel,claim)['passed']
        view=client.get('/api/poc-voc/runs/'+claim['run_id'],params={'period':'week2'}).json()
        payload={'baseline_id':other,'run_id':claim['run_id'],'period':'week2','answers':view['predictions'],
                 'reviewed_ids':[],'human_seconds':100,'idempotency_key':'wrong-baseline'}
        assert client.post('/api/poc-voc/assisted',json=payload).status_code==409


def test_poc_feedback_apply_and_repeat_on_same_and_next_inputs(kernel):
    with TestClient(app) as client:
        base=baseline(client)
        first, before=run(client,kernel,'week1','poc-before')
        answers=gold('week1')
        changed=[key for key in answers if answers[key]!=before['predictions'][key]]
        response=client.post('/api/poc-voc/assisted',json={'baseline_id':base,'run_id':first['run_id'],
            'period':'week1','answers':answers,'reviewed_ids':changed,'human_seconds':420,'idempotency_key':'poc-before-review'})
        assert response.status_code==200,response.text
        response=client.post('/api/feedback',json={'idempotency_key':'poc-feedback','artifact_id':before['artifact_id'],
            'recommendation_id':'REC-billing','reason':'누락','challenger_version':'challenger-v2'})
        assert response.status_code==200,response.text
        proposal=response.json()['proposal_id']
        assert client.post(f'/api/improvements/{proposal}/evaluate',json={'idempotency_key':'poc-evaluate'}).status_code==200
        result=next(x for x in client.get('/api/improvements').json() if x['proposal_id']==proposal)
        assert result['verdict']=='PASS' and not result['results']['regressions']
        assert client.post(f'/api/improvements/{proposal}/apply',json={'idempotency_key':'poc-apply'}).status_code==200
        second, after=run(client,kernel,'week1','poc-after')
        assert before['method_version']=='champion-v1' and after['method_version']=='challenger-v2'
        before_correct=sum(before['predictions'][key]==value for key,value in answers.items())
        after_correct=sum(after['predictions'][key]==value for key,value in answers.items())
        assert after_correct>before_correct
        assert client.post('/api/poc-voc/assisted',json={'baseline_id':base,'run_id':second['run_id'],
            'period':'week1','answers':after['predictions'],'reviewed_ids':[],'human_seconds':120,'idempotency_key':'poc-after-review'}).status_code==200
        third, following=run(client,kernel,'week2','poc-following')
        assert following['method_version']=='challenger-v2'
        assert client.post('/api/poc-voc/assisted',json={'baseline_id':base,'run_id':third['run_id'],
            'period':'week2','answers':following['predictions'],'reviewed_ids':[],'human_seconds':100,'idempotency_key':'poc-following-review'}).status_code==200
        assert [x['period'] for x in client.get('/api/poc-voc/measurements').json()]==['week1','week1','week1','week2']
        assert client.post(f'/api/improvements/{proposal}/rollback',json={'idempotency_key':'poc-rollback'}).status_code==200
        assert client.get('/api/voc/state').json()['method']['active_method_id']=='champion-v1'
