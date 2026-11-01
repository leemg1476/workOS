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


def test_registered_workload_selects_verifier_and_acceptance_hook(kernel, monkeypatch):
    from app.workloads import registry
    importlib.import_module('app.workloads.example_total')
    with engine.begin() as c:
        c.execute(text('''INSERT INTO task_definitions VALUES ('EXTENSION-HOOK',1,'total','sum','{}','{}',
            '{"kind":"extension_with_hook"}',true)'''))
        c.execute(text("INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type) VALUES ('EXTENSION-HOOK-JOB','EXTENSION-HOOK',1,'MANUAL')"))
    monkeypatch.setitem(registry.verifier_kind_resolvers,'extension_with_hook',lambda work:'example_total_v1')
    def on_accepted(kernel, c, work, run_id):
        kernel._event(c,'extension.accepted',work['work_id'],{'run_id':str(run_id)})
    monkeypatch.setitem(registry.acceptance_hooks,'example_total_v1',on_accepted)
    work,_=kernel.trigger('EXTENSION-HOOK-JOB','extension-hook',{'numbers':[2,3]})
    claim=kernel.claim()
    assert kernel.submit(claim['run_id'],{'total':5})['passed']
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM work_events WHERE event_type='extension.accepted' AND subject_id=:id"),{'id':str(work)}).scalar_one()==1
    def failing_hook(kernel, c, work, run_id):
        raise RuntimeError('follow-up failed')
    monkeypatch.setitem(registry.acceptance_hooks,'example_total_v1',failing_hook)
    work,_=kernel.trigger('EXTENSION-HOOK-JOB','extension-hook-failure',{'numbers':[2,3]})
    claim=kernel.claim()
    with pytest.raises(RuntimeError,match='follow-up failed'):
        kernel.submit(claim['run_id'],{'total':5})
    with engine.connect() as c:
        status=c.execute(text('SELECT status,accepted_artifact_id FROM work_items WHERE work_id=:id'),{'id':work}).mappings().one()
        assert status['status']=='RUNNING' and status['accepted_artifact_id'] is None
        assert c.execute(text('SELECT count(*) FROM artifacts WHERE run_id=:id'),{'id':claim['run_id']}).scalar_one()==0


def test_builtin_registrations_are_loaded_before_configured_extensions():
    from app.workloads import registry
    registry.load_configured()
    with pytest.raises(ValueError, match='duplicate'):
        registry.register_acceptance_hook('policy_impact_v1',lambda *args: None)
    with pytest.raises(ValueError, match='duplicate'):
        registry.register_verifier_kind_resolver('policy_impact_v1',lambda work:'example_total_v1')
