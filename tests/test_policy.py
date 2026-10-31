from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.domain.contracts import DocumentUpdatedRequest
from app.work.db import engine
from app.work.seed import seed
from app.worker import process_claim
from app.work.policy import make_policy_report
from app.work.voc import make_report
from app.work.service import Conflict
import pytest


def event_body(client, event_id='doc-event-1'):
    policy = client.get('/api/policies/POLICY-VOC').json()
    old, new = policy['versions']
    return {'event_id':event_id,'document_id':'POLICY-VOC','old_version':old['version'],'new_version':new['version'],
            'old_sha256':old['sha256'],'new_sha256':new['sha256']}


def test_event_to_review_dag_is_idempotent_and_verified(kernel):
    seed(engine)
    # Existing accepted dependent work must become stale when its pinned source changes.
    previous, _ = kernel.trigger('test-job', 'prior-work', {})
    prior_run = kernel.claim()
    kernel.submit(prior_run['run_id'], {'mode':'MOCK','task_id':'TEST','value':'accepted'})
    with engine.begin() as c:
        c.execute(text("INSERT INTO task_source_dependencies(task_id,document_id,pinned_version,status) VALUES ('TEST','POLICY-VOC',1,'CURRENT')"))
    with TestClient(app) as client:
        body = event_body(client)
        wrong = {**body, 'event_id':'bad-hash','new_sha256':'0'*64}
        assert client.post('/api/events/document.updated',json=wrong).status_code == 409
        req = DocumentUpdatedRequest(**body)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: kernel.document_updated(req), range(6)))
        assert sum(created for _,created in results) == 1
        assert len({work for work,_ in results}) == 1
        root = results[0][0]
        repeated = client.post('/api/events/document.updated',json={**body,'event_id':'other-delivery'}).json()
        assert repeated['work_id'] == root and repeated['created'] is False
        assert client.get(f'/api/work/{previous}').json()['work']['status'] == 'STALE'
        detail = client.get(f'/api/policy-work/{root}').json()
        assert [x['status'] for x in detail['followups']] == ['BLOCKED','BLOCKED','BLOCKED','WAITING_APPROVAL']
        assert len(detail['followups']) == 4
        assert detail['followups'][0]['predecessor_id'] == root
        assert detail['followups'][1]['predecessor_id'] == detail['followups'][0]['work_id']
        dependencies = client.get('/api/policies/POLICY-VOC').json()['dependencies']
        assert {d['task_id'] for d in dependencies if d['status']=='REVIEW_REQUIRED'} == {'TASK-003','TEST'}
        run = kernel.claim()
        assert run['work_id'] == root
        assert process_claim(kernel,run)['passed']
        detail = client.get(f'/api/policy-work/{root}').json()
        assert detail['work']['status'] == 'COMMITTED' and detail['run']['status'] == 'SUCCEEDED'
        assert detail['artifact']['verification']['passed']
        assert {x['clause_id'] for x in detail['artifact']['content']['changes']} == {'retention','review'}
        assert detail['artifact']['content']['review_status'] == 'REVIEW_REQUIRED'
        assert len(detail['artifact']['content']['changes'][0]['citations']) == 2
        first = kernel.claim()
        assert first['work_id'] == detail['followups'][0]['work_id']
        assert process_claim(kernel,first)['passed']
        detail = client.get(f'/api/policy-work/{root}').json()
        assert [x['status'] for x in detail['followups']] == ['COMMITTED','READY','BLOCKED','WAITING_APPROVAL']
        assert detail['followups'][0]['verification']['passed'] is True
        assert detail['followups'][0]['verification']['semantic_review'] == 'NOT_PERFORMED'
        note = {'idempotency_key':'note-1','artifact_id':detail['artifact']['artifact_id'],'note':'담당자 확인 필요'}
        assert client.post(f'/api/policy-work/{root}/review-notes',json=note).json()['recorded'] is True
        assert client.post(f'/api/policy-work/{root}/review-notes',json=note).json()['recorded'] is False
        assert client.post(f'/api/policy-work/{root}/review-notes',json={**note,'note':'다른 메모'}).status_code == 409
        assert client.get(f'/api/policy-work/{root}').json()['review_notes'][0]['payload']['decision'] == 'NOTE_ONLY'
        assert client.get('/api/policies/POLICY-VOC').json()['dependencies'][0]['status'] == 'REVIEW_REQUIRED'
        assert client.post(f'/api/work/{root}/revalidate',json={'idempotency_key':'review-again'}).json()['queued']
        assert client.post(f'/api/work/{root}/revalidate',json={'idempotency_key':'review-again'}).json()['queued'] is False
        again = kernel.claim()
        assert again['work_id'] == root and again['attempt_no'] == 2
        assert process_claim(kernel,again)['passed']
        detail = client.get(f'/api/policy-work/{root}').json()
        assert len(detail['followups']) == 4 and detail['followups'][0]['status'] == 'READY'
        assert client.post(f'/api/policy-work/{root}/review-notes',json={**note,'idempotency_key':'stale-note'}).status_code == 409
        recheck = kernel.claim()
        assert recheck['work_id'] == first['work_id'] and recheck['run_id'] != first['run_id']
        assert process_claim(kernel,recheck)['passed']


def test_tampered_policy_diff_cannot_be_committed(kernel):
    seed(engine)
    with TestClient(app) as client:
        body = event_body(client)
        root = client.post('/api/events/document.updated',json=body).json()['work_id']
        run = kernel.claim()
        context = kernel.prepare_policy(run['run_id'])
        report = make_policy_report(context)
        report['changes'][0]['after'] = '위조된 내용'
        result = kernel.submit(run['run_id'],report)
        assert result['passed'] is False and result['result']['checks']['clause_diff_and_locations'] is False
        detail = client.get(f'/api/policy-work/{root}').json()
        assert detail['work']['status'] == 'REWORK_REQUIRED'
        assert detail['work']['accepted_artifact_id'] is None


def test_followup_cannot_commit_fabricated_recheck(kernel):
    seed(engine)
    with TestClient(app) as client:
        root = client.post('/api/events/document.updated',json=event_body(client)).json()['work_id']
        assert process_claim(kernel,kernel.claim())['passed']
        run = kernel.claim()
        manifest = kernel.prepare_policy_verification(run['run_id'])
        forged = {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':1,
                  'parent_artifact_id':manifest['parent_artifact_id'],'parent_artifact_sha256':'0'*64,
                  'parent_context_sha256':manifest['parent_context_sha256'],
                  'verification':'DETERMINISTIC_RECHECK','semantic_review':'NOT_PERFORMED'}
        result = kernel.submit(run['run_id'],forged)
        assert not result['passed'] and not result['result']['checks']['receipt_matches']
        detail = client.get(f'/api/policy-work/{root}').json()
        assert detail['followups'][0]['status'] == 'REWORK_REQUIRED'
        assert detail['followups'][0]['artifact_id'] is None
        assert detail['followups'][1]['status'] == 'BLOCKED'
        assert kernel.retry(run['work_id'],'retry-followup')
        assert process_claim(kernel,kernel.claim())['passed']


def test_worker_recovers_preexisting_blocked_followup(kernel, monkeypatch):
    seed(engine)
    with TestClient(app) as client:
        root = client.post('/api/events/document.updated',json=event_body(client)).json()['work_id']
        schedule = kernel._schedule_policy_recheck
        monkeypatch.setattr(kernel,'_schedule_policy_recheck',lambda *args: False)
        assert process_claim(kernel,kernel.claim())['passed']
        monkeypatch.setattr(kernel,'_schedule_policy_recheck',schedule)
        assert client.get(f'/api/policy-work/{root}').json()['followups'][0]['status'] == 'BLOCKED'
        assert kernel.recover_policy_followups() == 1
        assert kernel.recover_policy_followups() == 0
        assert process_claim(kernel,kernel.claim())['passed']


def test_source_change_stops_inflight_dependent_run(kernel):
    seed(engine)
    work,_ = kernel.trigger('TASK-003-JOB','in-flight-voc',{})
    claim = kernel.claim()
    manifest = kernel.prepare_voc(claim['run_id'])
    with TestClient(app) as client:
        body = event_body(client)
        assert client.post('/api/events/document.updated',json=body).status_code == 200
        detail = client.get(f'/api/work/{work}').json()
        assert detail['work']['status'] == 'REWORK_REQUIRED'
        assert detail['runs'][0]['status'] == 'FAILED' and detail['runs'][0]['error'] == 'source became stale'
        with pytest.raises(Conflict):
            kernel.submit(claim['run_id'],make_report(manifest))
