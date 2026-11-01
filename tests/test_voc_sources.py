from app.workloads import voc_service
import os
from uuid import uuid4
from urllib.parse import quote
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.integrations.search import seed_index
from app.work.db import engine
from app.work.seed import seed
from app.work.voc import DOCUMENTS, make_report
from app.worker import process_claim


def start(kernel,key):
    work,_=kernel.trigger('TASK-003-JOB',key,{})
    claim=kernel.claim()
    assert claim['work_id']==work
    return work,claim


def test_voc_citation_is_bound_to_category_and_source(kernel):
    seed(engine)
    work,claim=start(kernel,'voc-source-forgery')
    manifest=voc_service.prepare_voc(kernel,claim['run_id'])
    assert manifest['retrieval']['mode']=='FIXTURE_DB'
    report=make_report(manifest)
    assert {d['id'] for d in manifest['documents']}==set(DOCUMENTS.values())
    report['recommendations'][0]['document_citations']=['DOC-VOC-SUPPORT@1']
    result=kernel.submit(claim['run_id'],report)
    assert not result['passed'] and not result['result']['checks']['document_citations']
    with TestClient(app) as client:
        assert client.get(f'/api/work/{work}').json()['work']['accepted_artifact_id'] is None


def test_voc_new_source_record_after_snapshot_prevents_publication(kernel):
    seed(engine)
    work,claim=start(kernel,'voc-record-changed')
    manifest=voc_service.prepare_voc(kernel,claim['run_id'])
    report=make_report(manifest)
    with engine.begin() as c:
        c.execute(text("INSERT INTO voc_records(record_id,source_version,body) VALUES ('VOC-NEW-01','voc-demo-v1','가상 추가 결제 문의')"))
    result=kernel.submit(claim['run_id'],report)
    assert not result['passed'] and not result['result']['checks']['records_source_current']
    with TestClient(app) as client:
        assert client.get(f'/api/work/{work}').json()['work']['accepted_artifact_id'] is None


def test_voc_opensearch_sources_missing_changed_and_recovered(kernel,monkeypatch):
    base=os.getenv('WORKOS_TEST_OPENSEARCH_URL')
    if not base:
        pytest.skip('Set WORKOS_TEST_OPENSEARCH_URL for OpenSearch integration')
    index='workos-test-'+uuid4().hex
    for key,value in {'WORKOS_SEARCH_MODE':'OPENSEARCH','OPENSEARCH_URL':base,'OPENSEARCH_INDEX':index}.items():
        monkeypatch.setenv(key,value)
    seed(engine)
    url=f'{base.rstrip("/")}/{index}'
    doc_url=f'{url}/_doc/{quote("DOC-VOC-BILLING@1",safe="")}'
    try:
        assert seed_index(engine,base,index)==6
        httpx.delete(doc_url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
        work,claim=start(kernel,'voc-source-missing')
        failed=process_claim(kernel,claim)
        assert not failed['passed'] and not failed['result']['checks']['document_versions_and_hashes']
        assert seed_index(engine,base,index)==6
        assert kernel.retry(work,'voc-source-retry')
        restored=kernel.claim()
        assert restored['run_id']!=claim['run_id'] and process_claim(kernel,restored)['passed']
        with TestClient(app) as client:
            artifact=client.get(f"/api/runs/{restored['run_id']}/artifacts").json()[0]
            assert artifact['content']['retrieval_mode']=='OPENSEARCH'
            assert all(item['document_citations']==[DOCUMENTS[item['category']]] for item in artifact['content']['recommendations'])
            assert artifact['evidence'][0]['result']['checks']['documents_search_current']
            assert client.get(f"/api/runs/{restored['run_id']}").json()['context']['manifest']['retrieval']['index']==index
        _,new=start(kernel,'voc-source-deleted-after-snapshot')
        manifest=voc_service.prepare_voc(kernel,new['run_id'])
        report=make_report(manifest)
        httpx.delete(doc_url,params={'refresh':'wait_for'},timeout=10).raise_for_status()
        late=kernel.submit(new['run_id'],report)
        assert not late['passed'] and not late['result']['checks']['documents_search_current']
        assert seed_index(engine,base,index)==6
        _,changed=start(kernel,'voc-source-db-changed')
        manifest=voc_service.prepare_voc(kernel,changed['run_id'])
        report=make_report(manifest)
        with engine.begin() as c:
            c.execute(text("UPDATE source_documents SET title='가상 변경' WHERE document_id='DOC-VOC-BILLING' AND version=1"))
        result=kernel.submit(changed['run_id'],report)
        assert not result['passed'] and not result['result']['checks']['documents_source_current']
    finally:
        httpx.delete(url,timeout=10).raise_for_status()
