from fastapi.testclient import TestClient
from app.api.main import app
from app.work.db import engine
from app.work.seed import seed
from app.work.service import Kernel
from app.worker import process_claim
from app.work.market import make_briefing
import pytest


def run_task(kernel, key, version='v1', omit=False):
    work, _ = kernel.trigger('TASK-001-JOB', key, {'dataset_version':version,'model_mode':'MOCK','omit_document':omit})
    claim = kernel.claim()
    assert claim['work_id'] == work
    outcome = process_claim(kernel, claim)
    return work, claim['run_id'], outcome


def test_market_comparison_and_citation_failure_recovery(kernel):
    seed(engine)
    _, first, a = run_task(kernel, 'market-v1')
    _, second, b = run_task(kernel, 'market-v2', 'v2')
    assert a['passed'] and b['passed']
    with TestClient(app) as client:
        comparison = client.get('/api/tasks/TASK-001/compare', params={'left':first,'right':second})
        assert comparison.status_code == 200
        fx = next(x for x in comparison.json()['changes'] if x['symbol'] == 'USD/KRW')
        assert fx == {'symbol':'USD/KRW','before':'1350','after':'1380','difference':'30'}
        artifact = client.get(f'/api/runs/{second}/artifacts').json()[0]
        assert artifact['artifact_version'] == 2
        fx_indicator = next(n for n in artifact['content']['indicators'] if n['symbol'] == 'USD/KRW')
        assert fx_indicator['change'] == '40' and fx_indicator['change_percent'] == '2.99'
        assert artifact['evidence'][0]['result']['checks']['numeric_recalculation'] is True
        assert artifact['evidence'][0]['result']['semantic_interpretation'] == 'NOT_VERIFIED'
        assert client.get(f'/api/runs/{second}').json()['context']['manifest']['query_result_sha256']
        work, failed_run, failure = run_task(kernel, 'missing-source', 'v2', True)
        assert failure['passed'] is False and failure['result']['checks']['citations_exist'] is False
        detail = client.get(f'/api/work/{work}').json()
        assert detail['work']['status'] == 'REWORK_REQUIRED' and detail['work']['accepted_artifact_id'] is None
        assert detail['runs'][0]['status'] == 'VERIFICATION_FAILED'
        dashboard = next(t for t in client.get('/api/tasks').json() if t['task_id'] == 'TASK-001')
        failed = next(x for x in dashboard['recent_verifications'] if x['run_id'] == str(failed_run))
        assert failed['verdict'] == 'FAIL' and not failed['is_current']
        assert 0 <= failed['checks_passed'] < failed['checks_total']
        assert failed['suite_version'] == 'market-suite-v3'
        assert any(x['run_id'] == str(second) and x['is_current'] for x in dashboard['recent_verifications'])
        assert kernel.retry(work, 'repair-source')
        retried = kernel.claim()
        assert retried['work_id'] == work and retried['run_id'] != failed_run
        assert process_claim(kernel, retried)['passed'] is True
        detail = client.get(f'/api/work/{work}').json()
        assert detail['work']['status'] == 'COMMITTED'
        assert [r['status'] for r in detail['runs']] == ['VERIFICATION_FAILED','SUCCEEDED']
        dashboard = next(t for t in client.get('/api/tasks').json() if t['task_id'] == 'TASK-001')
        repaired = next(x for x in dashboard['recent_verifications'] if x['run_id'] == str(retried['run_id']))
        assert repaired['verdict'] == 'PASS' and repaired['is_current']
        assert repaired['checks_passed'] == repaired['checks_total'] > 0


def test_numeric_tampering_never_commits(kernel):
    seed(engine)
    work, _ = kernel.trigger('TASK-001-JOB', 'bad-number', {'dataset_version':'v1','model_mode':'MOCK'})
    claim = kernel.claim()
    manifest = kernel.prepare_market(claim['run_id'])
    report, meta = make_briefing(manifest, 'MOCK')
    kernel.record_market_model(claim['run_id'], meta)
    report['indicators'][0]['current'] = '999999'
    result = kernel.submit(claim['run_id'], report)
    assert not result['passed'] and not result['result']['checks']['numeric_recalculation']
    with TestClient(app) as client:
        assert client.get(f'/api/work/{work}').json()['work']['accepted_artifact_id'] is None


def test_live_model_never_falls_back_to_mock(kernel, monkeypatch):
    seed(engine)
    _, _ = kernel.trigger('TASK-001-JOB', 'missing-key', {'dataset_version':'v1','model_mode':'LIVE_LLM'})
    claim = kernel.claim()
    manifest = kernel.prepare_market(claim['run_id'])
    for name in ('OPENAI_API_KEY','openai_api_key','OPENAI_MODEL','openai_model'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match='requires configured API key'):
        make_briefing(manifest, 'LIVE_LLM')
