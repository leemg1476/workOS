from app.workloads import policy_service
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from urllib.parse import quote
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.domain.contracts import PolicyReviewDecisionRequest
from app.evaluation.policy_regression import make_report
from app.integrations.search import seed_index
from app.work.db import engine
from app.work.seed import seed
from app.work.service import Conflict
from app.worker import process_claim


def test_regression_gates_retries_and_method_change_fences_running_work(kernel,monkeypatch):
    base=os.getenv('WORKOS_TEST_OPENSEARCH_URL')
    if not base:
        pytest.skip('Set WORKOS_TEST_OPENSEARCH_URL for isolated OpenSearch integration')
    index='workos-test-'+uuid4().hex
    for key,value in {'WORKOS_MODE':'MOCK','WORKOS_SEARCH_MODE':'OPENSEARCH','OPENSEARCH_URL':base,'OPENSEARCH_INDEX':index}.items():
        monkeypatch.setenv(key,value)
    seed(engine)
    try:
        assert seed_index(engine,base,index)==6
        with TestClient(app) as client:
            old,new=client.get('/api/policies/POLICY-VOC').json()['versions']
            body={'event_id':'regression-test','document_id':'POLICY-VOC','old_version':old['version'],
                  'new_version':new['version'],'old_sha256':old['sha256'],'new_sha256':new['sha256']}
            root=client.post('/api/events/document.updated',json=body).json()['work_id']
            for _ in range(3):
                assert process_claim(kernel,kernel.claim())['passed']
            detail=client.get(f'/api/policy-work/{root}').json()
            work=detail['followups'][2]['work_id']
            assert detail['followups'][2]['status']=='READY'
            assert not client.get(f'/api/policy-work/{root}/review-context').json()['ready']
            assert client.post(f'/api/policy-work/{root}/review-decision',json={'idempotency_key':'too-early',
                'context_sha256':'0'*64,'decision':'REVIEWED','note':'검토'}).status_code==409
            # Inject a known degraded method in this disposable DB to exercise the hard gate.
            with engine.begin() as c:
                c.execute(text("UPDATE voc_method_state SET active_method_id='challenger-regressed',version=version+1 WHERE task_id='TASK-003'"))
            failed=kernel.claim()
            bad=process_claim(kernel,failed)
            assert not bad['passed'] and not bad['result']['checks']['zero_classification_regressions']
            assert any(s['regressions'] for s in bad['result']['suite_results'])
            with engine.begin() as c:
                c.execute(text("UPDATE voc_method_state SET active_method_id='champion-v1',version=version+1 WHERE task_id='TASK-003'"))
            assert kernel.retry(work,'regression-retry')
            stale_run=kernel.claim()
            stale_manifest=policy_service.prepare_policy_regression(kernel,stale_run['run_id'])
            url=f'{base.rstrip("/")}/{index}/_doc/{quote("POLICY-VOC@2",safe="")}'
            indexed=httpx.get(url,timeout=10).json()['_source']
            httpx.delete(url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
            stale_result=kernel.submit(stale_run['run_id'],make_report(stale_manifest))
            assert not stale_result['passed'] and not stale_result['result']['checks']['index_current']
            httpx.put(url,params={'refresh':'wait_for'},json=indexed,timeout=10).raise_for_status()
            assert kernel.retry(work,'regression-retry-after-missing-index')
            forged_run=kernel.claim()
            manifest=policy_service.prepare_policy_regression(kernel,forged_run['run_id'])
            forged=make_report(manifest)
            forged['suites'][0]['active_correct']+=1
            result=kernel.submit(forged_run['run_id'],forged)
            assert not result['passed'] and not result['result']['checks']['report_recomputed']
            assert kernel.retry(work,'regression-retry-after-forgery')
            assert process_claim(kernel,kernel.claim())['passed']
            detail=client.get(f'/api/policy-work/{root}').json()
            verified=detail['followups'][2]['verification']
            assert [s['case_count'] for s in verified['suite_results']]==[24,6]
            assert verified['scope']=='FIXED_VOC_CLASSIFICATION_ONLY_NOT_POLICY_SEMANTICS'
            context=client.get(f'/api/policy-work/{root}/review-context').json()
            assert context['ready'] and len(context['context']['stages'])==4
            decision={'idempotency_key':'review-first','context_sha256':context['context_sha256'],
                      'decision':'REVIEWED','note':'가상 문서 검토'}
            assert client.post(f'/api/policy-work/{root}/review-decision',json={**decision,'context_sha256':'0'*64}).status_code==409
            old_url=f'{base.rstrip("/")}/{index}/_doc/{quote("POLICY-VOC@1",safe="")}'
            old_indexed=httpx.get(old_url,timeout=10).json()['_source']
            httpx.delete(old_url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
            assert client.post(f'/api/policy-work/{root}/review-decision',json=decision).status_code==409
            httpx.put(old_url,params={'refresh':'wait_for'},json=old_indexed,timeout=10).raise_for_status()
            current_doc=httpx.get(url,timeout=10).json()['_source']
            httpx.delete(url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
            assert client.post(f'/api/policy-work/{root}/review-decision',json=decision).status_code==409
            httpx.put(url,params={'refresh':'wait_for'},json=current_doc,timeout=10).raise_for_status()
            recorded=client.post(f'/api/policy-work/{root}/review-decision',json=decision).json()
            assert recorded['recorded'] and not recorded['policy_applied']
            assert not client.post(f'/api/policy-work/{root}/review-decision',json=decision).json()['recorded']
            assert client.post(f'/api/policy-work/{root}/review-decision',json={**decision,'note':'다른 내용'}).status_code==409
            review=client.get(f'/api/policy-work/{root}').json()
            assert review['followups'][3]['status']=='COMMITTED' and review['reviews'][0]['is_current']
            assert review['reviews'][0]['content']['decision']=='REVIEWED' and not review['reviews'][0]['content']['policy_applied']
            assert review['reviews'][0]['result']['scope']=='RECORD_INTEGRITY_ONLY_NOT_SEMANTIC_OR_POLICY_APPROVAL'
            human_run=client.get(f"/api/runs/{review['reviews'][0]['run_id']}").json()
            assert human_run['model_id']=='HUMAN' and human_run['status']=='SUCCEEDED'
            assert human_run['context']['manifest']['review_context_sha256']==context['context_sha256']
            with engine.connect() as c:
                actor=c.execute(text("SELECT actor FROM work_events WHERE event_type='policy.review_decided' AND subject_id=:id"),
                                {'id':str(review['followups'][3]['work_id'])}).scalar_one()
            assert actor=='user'
            assert client.post(f"/api/work/{review['followups'][3]['work_id']}/revalidate",json={'idempotency_key':'bad-review-revalidate'}).status_code==409
            reopened=client.post(f'/api/policy-work/{root}/review-reopen',json={'idempotency_key':'review-reopen'})
            assert reopened.json()['reopened']
            assert not client.post(f'/api/policy-work/{root}/review-reopen',json={'idempotency_key':'review-reopen'}).json()['reopened']
            context2=client.get(f'/api/policy-work/{root}/review-context').json()
            assert client.post(f'/api/policy-work/{root}/review-decision',json={**decision,'idempotency_key':'old-context'}).status_code==409
            changes={'idempotency_key':'review-changes','context_sha256':context2['context_sha256'],
                     'decision':'CHANGES_REQUESTED','note':'보완 필요'}
            with ThreadPoolExecutor(max_workers=4) as pool:
                deliveries=list(pool.map(lambda _:policy_service.decide_policy_review(kernel,root,PolicyReviewDecisionRequest(**changes)),range(4)))
            assert sum(created for _,created in deliveries)==1 and len({item[0] for item in deliveries})==1
            assert not client.post(f'/api/policy-work/{root}/review-decision',json=changes).json()['recorded']
            assert len(client.get(f'/api/policy-work/{root}').json()['reviews'])==2
            # Apply a real evaluated candidate while an old-method regression Run is active.
            voc,_=kernel.trigger('TASK-003-JOB','regression-voc',{})
            voc_run=kernel.claim()
            voc_result=process_claim(kernel,voc_run)
            feedback=client.post('/api/feedback',json={'idempotency_key':'regression-feedback',
                'artifact_id':voc_result['artifact_id'],'recommendation_id':'REC-billing','reason':'누락',
                'challenger_version':'challenger-v2'}).json()
            proposal=feedback['proposal_id']
            assert client.post(f'/api/improvements/{proposal}/evaluate',json={'idempotency_key':'regression-eval'}).status_code==200
            assert kernel.revalidate(work,'regression-inflight')
            old_run=kernel.claim()
            old_manifest=policy_service.prepare_policy_regression(kernel,old_run['run_id'])
            assert client.post(f'/api/improvements/{proposal}/apply',json={'idempotency_key':'regression-apply'}).status_code==200
            assert client.get(f'/api/policy-work/{root}').json()['followups'][3]['status']=='STALE'
            assert not client.get(f'/api/policy-work/{root}/review-context').json()['ready']
            assert client.post(f'/api/policy-work/{root}/review-decision',json={**changes,'idempotency_key':'stale-review'}).status_code==409
            with pytest.raises(Conflict):
                kernel.submit(old_run['run_id'],make_report(old_manifest))
            next_run=kernel.claim()
            assert next_run['work_id']==work and next_run['run_id']!=old_run['run_id']
            assert process_claim(kernel,next_run)['passed']
            detail=client.get(f'/api/policy-work/{root}').json()
            assert detail['followups'][3]['status']=='WAITING_APPROVAL' and not any(x['is_current'] for x in detail['reviews'])
            artifact=client.get(f"/api/runs/{next_run['run_id']}/artifacts").json()[0]
            assert artifact['content']['active_method']=='challenger-v2'
            assert client.post(f'/api/improvements/{proposal}/rollback',json={'idempotency_key':'regression-rollback'}).status_code==200
            final=kernel.claim()
            assert final['work_id']==work and process_claim(kernel,final)['passed']
            artifact=client.get(f"/api/runs/{final['run_id']}/artifacts").json()[0]
            assert artifact['content']['active_method']=='champion-v1'
            statuses=[r['status'] for r in client.get(f'/api/work/{work}').json()['runs']]
            assert statuses==['VERIFICATION_FAILED','VERIFICATION_FAILED','VERIFICATION_FAILED','SUCCEEDED','FAILED','SUCCEEDED','SUCCEEDED']
            assert client.get('/api/policies/POLICY-VOC').json()['dependencies'][0]['status']=='REVIEW_REQUIRED'
    finally:
        httpx.delete(f'{base.rstrip("/")}/{index}',timeout=10).raise_for_status()
