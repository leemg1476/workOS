import importlib
from sqlalchemy import text
from app.work.db import engine
from app.worker import process_claim
from app.work.service import Conflict
import pytest
from fastapi.testclient import TestClient
from app.api.main import app


def test_new_workload_executes_and_rejects_tampering(kernel):
    importlib.import_module('app.workloads.example_total')
    with engine.begin() as c:
        c.execute(text('''INSERT INTO task_definitions VALUES ('EXAMPLE-TOTAL',1,'total','sum','{}','{}',
            '{"kind":"example_total_v1"}',true)'''))
        c.execute(text("INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type) VALUES ('EXAMPLE-TOTAL-JOB','EXAMPLE-TOTAL',1,'MANUAL')"))
    work, _ = kernel.trigger('EXAMPLE-TOTAL-JOB', 'extension-success', {'numbers':[2, 3, 5]})
    claim = kernel.claim()
    result = process_claim(kernel, claim)
    assert result['passed'] is True
    with engine.connect() as c:
        row = c.execute(text('''SELECT w.status,a.content,e.verifier_version,ev.verdict FROM work_items w
            JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id
            JOIN evidence e ON e.artifact_id=a.artifact_id JOIN evaluations ev ON ev.artifact_id=a.artifact_id
            WHERE w.work_id=:id'''), {'id':work}).mappings().one()
        assert row['status'] == 'COMMITTED' and row['content']['total'] == 10
        assert row['verifier_version'] == 'example-total-validator-v1' and row['verdict'] == 'PASS'
    work, _ = kernel.trigger('EXAMPLE-TOTAL-JOB', 'extension-fail', {'numbers':[2, 3, 5]})
    claim = kernel.claim()
    assert kernel.submit(claim['run_id'], {'total':999})['passed'] is False
    with engine.connect() as c:
        row = c.execute(text('SELECT status,accepted_artifact_id FROM work_items WHERE work_id=:id'), {'id':work}).mappings().one()
        assert row['status'] == 'REWORK_REQUIRED' and row['accepted_artifact_id'] is None
    with TestClient(app) as client:
        response = client.post('/api/tasks/EXAMPLE-TOTAL/run', json={
            'idempotency_key':'extension-api', 'input_snapshot':{'numbers':[7, 8]}})
        assert response.status_code == 200
        api_work = response.json()['work_id']
        api_claim = kernel.claim()
        assert str(api_claim['work_id']) == api_work
        assert process_claim(kernel, api_claim)['passed']
        assert client.get(f'/api/work/{api_work}').json()['work']['status'] == 'COMMITTED'
        artifact = client.get(f"/api/runs/{api_claim['run_id']}/artifacts").json()[0]
        assert artifact['content']['total'] == 15


def test_unknown_validator_fails_closed(kernel):
    with engine.begin() as c:
        c.execute(text("UPDATE task_definitions SET acceptance_policy=CAST('{\"kind\":\"unknown\"}' AS jsonb) WHERE task_id='TEST'"))
    kernel.trigger('test-job', 'unknown-extension', {})
    claim = kernel.claim()
    with pytest.raises(Conflict, match='validator is not implemented'):
        kernel.submit(claim['run_id'], {})
