import hashlib
import os
from uuid import uuid4
from urllib.parse import quote
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from app.work.db import engine
from app.work.seed import seed
from app.work.market import make_briefing
from app.integrations.search import seed_index
from app.worker import process_claim
from fastapi.testclient import TestClient
from app.api.main import app


def test_authoritative_source_change_prevents_publication(kernel):
    seed(engine)
    work,_ = kernel.trigger('TASK-001-JOB','changed-document',{'dataset_version':'v1','model_mode':'MOCK'})
    run = kernel.claim()
    manifest = kernel.prepare_market(run['run_id'])
    report, metadata = make_briefing(manifest,'MOCK')
    kernel.record_market_model(run['run_id'],metadata)
    changed = '가상 문서가 준비 후 변경됨'
    with engine.begin() as c:
        c.execute(text("UPDATE source_documents SET body=:body,sha256=:sha WHERE document_id='DOC-FX' AND version=1"),
                  {'body':changed,'sha':hashlib.sha256(changed.encode('utf-8')).hexdigest()})
    result = kernel.submit(run['run_id'],report)
    assert not result['passed'] and not result['result']['checks']['source_current']


def test_real_opensearch_missing_citation_and_recovery(kernel,monkeypatch):
    base=os.getenv('WORKOS_TEST_OPENSEARCH_URL')
    if not base:
        pytest.skip('Set WORKOS_TEST_OPENSEARCH_URL for OpenSearch integration')
    seed(engine)
    index='workos-test-'+uuid4().hex
    monkeypatch.setenv('WORKOS_SEARCH_MODE','OPENSEARCH')
    monkeypatch.setenv('OPENSEARCH_URL',base)
    monkeypatch.setenv('OPENSEARCH_INDEX',index)
    index_url=f'{base.rstrip("/")}/{index}'
    try:
        assert seed_index(engine,base,index) == 3
        work,_ = kernel.trigger('TASK-001-JOB','search-good',{'dataset_version':'v1','model_mode':'MOCK'})
        first=kernel.claim()
        assert process_claim(kernel,first)['passed']
        with engine.connect() as c:
            manifest=c.execute(text('SELECT manifest FROM run_contexts WHERE run_id=:id'),{'id':first['run_id']}).scalar_one()
        assert manifest['retrieval']['mode']=='OPENSEARCH' and manifest['retrieval']['index']==index
        httpx.delete(f'{index_url}/_doc/{quote("DOC-FX@1",safe="")}?refresh=wait_for',timeout=5).raise_for_status()
        bad,_=kernel.trigger('TASK-001-JOB','search-missing',{'dataset_version':'v2','model_mode':'MOCK'})
        failed=kernel.claim()
        result=process_claim(kernel,failed)
        assert not result['passed'] and not result['result']['checks']['citations_exist']
        assert seed_index(engine,base,index) == 3
        kernel.retry(bad,'search-repair')
        retried=kernel.claim()
        assert retried['work_id']==bad and retried['run_id']!=failed['run_id']
        assert process_claim(kernel,retried)['passed']
        changed,_=kernel.trigger('TASK-001-JOB','search-changed-after-context',{'dataset_version':'v1','model_mode':'MOCK'})
        active=kernel.claim()
        manifest=kernel.prepare_market(active['run_id'])
        report,metadata=make_briefing(manifest,'MOCK')
        kernel.record_market_model(active['run_id'],metadata)
        httpx.delete(f'{index_url}/_doc/{quote("DOC-FX@1",safe="")}?refresh=wait_for',timeout=5).raise_for_status()
        outcome=kernel.submit(active['run_id'],report)
        assert not outcome['passed'] and outcome['result']['checks']['citations_exist']
        assert not outcome['result']['checks']['search_current']
    finally:
        httpx.delete(index_url,timeout=10)


def test_source_reader_can_select_but_cannot_update(kernel,monkeypatch):
    seed(engine)
    reader_url=engine.url.set(username='workos_demo_reader',password='workos_demo_reader').render_as_string(hide_password=False)
    reader=create_engine(reader_url)
    try:
        with reader.connect() as c:
            assert c.execute(text('SELECT count(*) FROM market_observations')).scalar_one() == 4
        with pytest.raises(DBAPIError):
            with reader.begin() as c:
                c.execute(text("UPDATE market_observations SET current_value=0 WHERE symbol='nonexistent'"))
        monkeypatch.setenv('WORKOS_SOURCE_DATABASE_URL',reader_url)
        work,_=kernel.trigger('TASK-001-JOB','reader-run',{'dataset_version':'v1','model_mode':'MOCK'})
        run=kernel.claim()
        assert process_claim(kernel,run)['passed']
        with engine.connect() as c:
            manifest=c.execute(text('SELECT manifest FROM run_contexts WHERE run_id=:id'),{'id':run['run_id']}).scalar_one()
        assert manifest['sql_source_mode']=='POSTGRES_READ_ONLY_ROLE'
    finally:
        reader.dispose()


def test_policy_search_requires_indexed_previous_version(kernel,monkeypatch):
    base=os.getenv('WORKOS_TEST_OPENSEARCH_URL')
    if not base:
        pytest.skip('Set WORKOS_TEST_OPENSEARCH_URL for OpenSearch integration')
    seed(engine)
    index='workos-test-'+uuid4().hex
    monkeypatch.setenv('WORKOS_SEARCH_MODE','OPENSEARCH')
    monkeypatch.setenv('OPENSEARCH_URL',base)
    monkeypatch.setenv('OPENSEARCH_INDEX',index)
    index_url=f'{base.rstrip("/")}/{index}'
    try:
        assert seed_index(engine,base,index)==3
        assert httpx.get(f'{index_url}/_doc/{quote("POLICY-VOC@2",safe="")}',timeout=5).status_code == 404
        with TestClient(app) as client:
            versions=client.get('/api/policies/POLICY-VOC').json()['versions']
            body={'event_id':'policy-search-test','document_id':'POLICY-VOC','old_version':1,'new_version':2,
                  'old_sha256':versions[0]['sha256'],'new_sha256':versions[1]['sha256']}
            root=client.post('/api/events/document.updated',json=body).json()['work_id']
            httpx.delete(f'{index_url}/_doc/{quote("POLICY-VOC@1",safe="")}?refresh=wait_for',timeout=5).raise_for_status()
            first=kernel.claim()
            failed=process_claim(kernel,first)
            assert not failed['passed'] and not failed['result']['checks']['previous_version_retrieval']
            seed_index(engine,base,index)
            kernel.retry(root,'policy-index-repair')
            second=kernel.claim()
            assert process_claim(kernel,second)['passed']
            manifest=client.get(f"/api/runs/{second['run_id']}").json()['context']['manifest']
            assert manifest['retrieval']['mode']=='OPENSEARCH'
            assert manifest['indexed_previous_document'][0]['id']=='POLICY-VOC@1'
            assert httpx.get(f'{index_url}/_doc/{quote("POLICY-VOC@2",safe="")}',timeout=5).status_code == 404
    finally:
        httpx.delete(index_url,timeout=10)
