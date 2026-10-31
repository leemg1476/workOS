"""Opt-in paid LIVE_LLM smoke test against a disposable running demo stack."""
import os
import time
import uuid
import httpx

if os.getenv('WORKOS_LIVE_SMOKE_CONFIRM') != 'disposable-paid-call':
    raise SystemExit('Use a disposable stack; set WORKOS_LIVE_SMOKE_CONFIRM=disposable-paid-call')
base = os.getenv('WORKOS_E2E_BASE', 'http://localhost:8000/api')
with httpx.Client(base_url=base, timeout=10) as client:
    response = client.post('/tasks/TASK-001/run', json={'idempotency_key':str(uuid.uuid4()),
        'input_snapshot':{'dataset_version':'v1','model_mode':'LIVE_LLM'}})
    response.raise_for_status()
    work_id = response.json()['work_id']
    for _ in range(120):
        response = client.get(f'/work/{work_id}')
        response.raise_for_status()
        detail = response.json()
        if detail['work']['status'] in ('COMMITTED', 'REWORK_REQUIRED'):
            break
        time.sleep(.5)
    else:
        raise AssertionError('live run did not finish')
    run = detail['runs'][-1]
    assert detail['work']['status'] == 'COMMITTED', f"live run failed: {run['status']} {run['error']}"
    artifacts = client.get(f"/runs/{run['run_id']}/artifacts").json()
    steps = client.get(f"/runs/{run['run_id']}").json()['steps']
    artifact = artifacts[-1]
    assert artifact['content']['model_mode'] == 'LIVE_LLM' and artifact['content']['data_mode'] == 'DEMO DATA'
    assert artifact['evidence'][0]['result']['passed'] and steps[-1]['status'] == 'SUCCEEDED'
    assert run['model_id'] != 'MOCK'
    print(f"LIVE_LLM model={run['model_id']} data=DEMO DATA verifier=PASS graph={steps[-1]['graph_version']}")
