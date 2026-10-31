from fastapi.testclient import TestClient
from app.api.main import app
from app.work.service import Kernel
from app.work.db import engine


def test_seed_dashboard_and_observable_failure(kernel):
    with TestClient(app) as client:
        tasks = client.get('/api/tasks').json()
        assert {t['task_id'] for t in tasks} >= {'TASK-001', 'TASK-002', 'TASK-003'}
        response = client.post('/api/tasks/TASK-001/run', json={'idempotency_key':'api-test','input_snapshot':{}})
        assert response.status_code == 200
        work_id = response.json()['work_id']
        claim = Kernel(engine).claim()
        assert claim['work_id'] == work_id
        Kernel(engine).fail(claim['run_id'], 'MOCK: task workload and acceptance validator are not implemented')
        detail = client.get(f'/api/work/{work_id}').json()
        assert detail['work']['status'] == 'REWORK_REQUIRED'
        assert detail['runs'][0]['status'] == 'FAILED'
        assert 'not implemented' in detail['runs'][0]['error']
        task = next(t for t in client.get('/api/tasks').json() if t['task_id'] == 'TASK-001')
        assert task['recent_verifications'][0]['run_status'] == 'FAILED'
        assert task['recent_verifications'][0]['verdict'] is None
        assert task['recent_verifications'][0]['checks_total'] == 0
        assert task['recent_verifications'][0]['is_current'] is False
        assert client.get('/api/health').json()['mode'] == 'MOCK'
