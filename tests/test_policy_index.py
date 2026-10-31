import os
from uuid import uuid4
from urllib.parse import quote
import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.api.main import app
from app.domain.contracts import PolicyIndexReceipt
from app.integrations.search import seed_index, index_demo_policy, demo_index_target, IndexConflict, SearchUnavailable
from app.work.db import engine
from app.work.seed import seed
from app.worker import process_claim


def test_policy_index_refuses_remote_or_non_demo_target(monkeypatch):
    monkeypatch.setenv('WORKOS_MODE','MOCK')
    monkeypatch.setenv('WORKOS_SEARCH_MODE','OPENSEARCH')
    monkeypatch.setenv('OPENSEARCH_INDEX','workos-demo-documents-v1')
    monkeypatch.setenv('OPENSEARCH_URL','http://example.com:9200')
    with pytest.raises(SearchUnavailable):
        demo_index_target()
    monkeypatch.setenv('OPENSEARCH_URL','http://localhost:9200')
    monkeypatch.setenv('OPENSEARCH_INDEX','customer-records')
    with pytest.raises(SearchUnavailable):
        demo_index_target()


def test_index_receipt_rejects_unverified_extra_claims():
    receipt={'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':2,
             'predecessor_artifact_id':'example','document_id':'POLICY-VOC',
             'document_version':2,'document_sha256':'0'*64,'index':'workos-demo-documents-v1',
             'operation':'CREATED','approved':True}
    with pytest.raises(ValidationError):
        PolicyIndexReceipt.model_validate(receipt)


def test_index_is_verified_retried_and_revalidated(kernel,monkeypatch):
    base = os.getenv('WORKOS_TEST_OPENSEARCH_URL')
    if not base:
        pytest.skip('Set WORKOS_TEST_OPENSEARCH_URL for isolated OpenSearch integration')
    index = 'workos-test-'+uuid4().hex
    monkeypatch.setenv('WORKOS_MODE','MOCK')
    monkeypatch.setenv('WORKOS_SEARCH_MODE','OPENSEARCH')
    monkeypatch.setenv('OPENSEARCH_URL',base)
    monkeypatch.setenv('OPENSEARCH_INDEX',index)
    index_url=f'{base.rstrip("/")}/{index}'
    doc_url=f'{index_url}/_doc/{quote("POLICY-VOC@2",safe="")}'
    seed(engine)
    try:
        assert seed_index(engine,base,index) == 6
        with TestClient(app) as client:
            versions=client.get('/api/policies/POLICY-VOC').json()['versions']
            old,new=versions
            event={'event_id':'policy-index-test','document_id':'POLICY-VOC','old_version':old['version'],
                   'new_version':new['version'],'old_sha256':old['sha256'],'new_sha256':new['sha256']}
            root=client.post('/api/events/document.updated',json=event).json()['work_id']
            assert process_claim(kernel,kernel.claim())['passed']
            assert process_claim(kernel,kernel.claim())['passed']
            run=kernel.claim()
            manifest=kernel.prepare_policy_index(run['run_id'])
            doc=manifest['document']
            assert index_demo_policy(doc,index)=='CREATED'
            assert index_demo_policy(doc,index)=='ALREADY_PRESENT'
            corrupt={**doc,'body':'가상 인덱스 변조'}
            httpx.put(doc_url,params={'refresh':'wait_for'},json=corrupt,timeout=10).raise_for_status()
            with pytest.raises(IndexConflict):
                index_demo_policy(doc,index)
            receipt={'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':2,
                     'predecessor_artifact_id':manifest['predecessor_artifact_id'],
                     'document_id':doc['document_id'],'document_version':doc['version'],
                     'document_sha256':doc['sha256'],'index':index,'operation':'CREATED'}
            result=kernel.submit(run['run_id'],receipt)
            assert not result['passed'] and not result['result']['checks']['indexed_version_and_hash']
            detail=client.get(f'/api/policy-work/{root}').json()
            assert detail['followups'][1]['status']=='REWORK_REQUIRED' and detail['followups'][1]['artifact_id'] is None
            httpx.delete(doc_url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
            assert kernel.retry(run['work_id'],'retry-index')
            second=kernel.claim()
            assert second['run_id']!=run['run_id'] and process_claim(kernel,second)['passed']
            detail=client.get(f'/api/policy-work/{root}').json()
            check=detail['followups'][1]['verification']
            assert detail['followups'][1]['status']=='COMMITTED' and check['checks']['previous_version_preserved']
            assert check['indexed_document']['id']=='POLICY-VOC@2'
            assert client.post(f'/api/work/{root}/revalidate',json={'idempotency_key':'policy-index-revalidate'}).json()['queued']
            assert process_claim(kernel,kernel.claim())['passed']
            assert process_claim(kernel,kernel.claim())['passed']
            replay=kernel.claim()
            assert replay['work_id']==run['work_id'] and process_claim(kernel,replay)['passed']
            artifacts=client.get(f"/api/runs/{replay['run_id']}/artifacts").json()
            assert artifacts[0]['content']['operation']=='ALREADY_PRESENT'
            history=client.get(f"/api/work/{run['work_id']}").json()
            assert [item['status'] for item in history['runs']]==['VERIFICATION_FAILED','SUCCEEDED','SUCCEEDED']
            assert detail['followups'][2]['status']=='READY'
    finally:
        httpx.delete(index_url,timeout=10).raise_for_status()
