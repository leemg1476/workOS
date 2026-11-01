from app.workloads import voc_service
from fastapi.testclient import TestClient
from sqlalchemy import text
import json
from app.api.main import app
from app.work.db import engine
from app.work.seed import seed
from app.work.market import digest
from app.work.voc import make_report
from app.worker import process_claim


def start(kernel,key):
    work,_=kernel.trigger('TASK-003-JOB',key,{})
    run=kernel.claim()
    assert run['work_id']==work
    return work,run


def test_history_snapshot_tamper_and_retry(kernel):
    seed(engine)
    first_work,first=start(kernel,'history-first')
    assert process_claim(kernel,first)['passed']
    with TestClient(app) as client:
        initial=client.get(f"/api/runs/{first['run_id']}/artifacts").json()[0]
        assert initial['schema_version']=='3'
        assert initial['content']['previous_artifact_id'] is None
        assert {r['comparison'] for r in initial['content']['recommendations']}=={'NO_PREVIOUS_REPORT'}
        work,run=start(kernel,'history-second')
        manifest=voc_service.prepare_voc(kernel,run['run_id'])
        assert manifest['previous_report']['artifact_id']==initial['artifact_id']
        report=make_report(manifest)
        assert report['comparison_basis']=='CATEGORY_ONLY_NOT_SEMANTIC'
        assert {r['comparison'] for r in report['recommendations']}=={'SAME_CATEGORY'}
        assert all(r['previous_recommendation_id']==r['recommendation_id'] for r in report['recommendations'])
        report['recommendations'][0]['semantic_duplicate']=True
        false_claim=kernel.submit(run['run_id'],report)
        assert not false_claim['passed'] and not false_claim['result']['checks']['schema']
        assert kernel.retry(work,'history-retry-after-claim')
        run=kernel.claim()
        manifest=voc_service.prepare_voc(kernel,run['run_id'])
        report=make_report(manifest)
        report['recommendations'][0]['comparison']='NEW_CATEGORY'
        failure=kernel.submit(run['run_id'],report)
        assert not failure['passed'] and not failure['result']['checks']['comparison_recomputed']
        assert client.get(f'/api/work/{work}').json()['work']['accepted_artifact_id'] is None
        assert client.get(f'/api/work/{first_work}').json()['work']['accepted_artifact_id']==initial['artifact_id']
        assert kernel.retry(work,'history-retry')
        retried=kernel.claim()
        result=process_claim(kernel,retried)
        assert result['passed'] and retried['run_id']!=run['run_id']
        artifact=client.get(f"/api/runs/{retried['run_id']}/artifacts").json()[0]
        assert artifact['content']['previous_artifact_id']==initial['artifact_id']
        assert artifact['evidence'][0]['result']['checks']['previous_artifact_verified']
        context=client.get(f"/api/runs/{retried['run_id']}").json()['context']['manifest']
        assert context['previous_report']['sha256']==initial['sha256']
        assert context['previous_report']['evidence_id']==initial['evidence'][0]['evidence_id']
        assert context['previous_report']['evaluation_id'] and context['previous_report']['verifier_version']=='voc-validator-v3'
        assert artifact['content']['history_as_of']==context['history_as_of']


def test_changed_prior_artifact_cannot_be_verified(kernel):
    seed(engine)
    _,first=start(kernel,'history-integrity-first')
    original=process_claim(kernel,first)['artifact_id']
    _,second=start(kernel,'history-integrity-second')
    manifest=voc_service.prepare_voc(kernel,second['run_id'])
    report=make_report(manifest)
    with engine.begin() as c:
        c.execute(text("UPDATE artifacts SET sha256=:hash WHERE artifact_id=:id"),{'hash':'0'*64,'id':original})
    result=kernel.submit(second['run_id'],report)
    assert not result['passed'] and not result['result']['checks']['previous_artifact_verified']


def test_new_category_is_distinguished_from_a_repeated_category(kernel):
    seed(engine)
    rules={'billing':['결제','청구'],'delivery':['배송','택배'],'support':[]}
    with engine.begin() as c:
        c.execute(text('''INSERT INTO voc_method_versions(method_id,changed_component,rules,sha256)
            VALUES ('test-missing-support','classification_rules',CAST(:rules AS jsonb),:sha)'''),
            {'rules':json.dumps(rules,ensure_ascii=False),'sha':digest(rules)})
        c.execute(text("UPDATE voc_method_state SET active_method_id='test-missing-support',version=version+1 WHERE task_id='TASK-003'"))
    _,first=start(kernel,'history-two-categories')
    assert process_claim(kernel,first)['passed']
    with engine.begin() as c:
        c.execute(text("UPDATE voc_method_state SET active_method_id='champion-v1',version=version+1 WHERE task_id='TASK-003'"))
    _,second=start(kernel,'history-new-category')
    result=process_claim(kernel,second)
    assert result['passed']
    with TestClient(app) as client:
        rows=client.get(f"/api/runs/{second['run_id']}/artifacts").json()[0]['content']['recommendations']
        comparisons={row['category']:row['comparison'] for row in rows}
        assert comparisons=={'billing':'SAME_CATEGORY','delivery':'SAME_CATEGORY','support':'NEW_CATEGORY'}
        assert next(row for row in rows if row['category']=='support')['previous_recommendation_id'] is None


def test_preexisting_v2_work_executes_and_v4_reuses_its_history(kernel):
    seed(engine)
    with engine.begin() as c:
        schema=c.execute(text("SELECT output_schema FROM task_definitions WHERE task_id='TASK-003' AND version=4 AND active")).scalar_one()
        assert schema['additionalProperties'] is False and 'previous_artifact_id' in schema['properties']
        c.execute(text("UPDATE job_definitions SET task_version=2 WHERE job_id='TASK-003-JOB'"))
    old_work,old=start(kernel,'old-contract')
    assert old['task_version']==2
    with engine.begin() as c:
        c.execute(text("UPDATE job_definitions SET task_version=4 WHERE job_id='TASK-003-JOB'"))
    old_result=process_claim(kernel,old)
    assert old_result['passed']
    _,new=start(kernel,'new-contract')
    assert new['task_version']==4
    new_result=process_claim(kernel,new)
    assert new_result['passed']
    with TestClient(app) as client:
        old_artifact=client.get(f"/api/runs/{old['run_id']}/artifacts").json()[0]
        assert old_artifact['schema_version']=='1'
        old_report=old_artifact['content']
        new_report=client.get(f"/api/runs/{new['run_id']}/artifacts").json()[0]['content']
        assert 'comparison_basis' not in old_report
        assert new_report['previous_artifact_id']==old_result['artifact_id']
        assert {r['comparison'] for r in new_report['recommendations']}=={'SAME_CATEGORY'}
        assert client.get(f'/api/work/{old_work}').json()['work']['task_version']==2
