import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from sqlalchemy import text
from app.runtime.graph import GraphRunner
from app.work.db import engine
from app.work.seed import seed


def test_checkpoint_resume_keeps_completed_draft_and_kernel_accepts(kernel):
    seed(engine)
    work, _ = kernel.trigger('TASK-001-JOB', 'graph-resume', {'dataset_version':'v1','model_mode':'MOCK'})
    claim = kernel.claim(lease_seconds=120)
    manifest = kernel.prepare_market(claim['run_id'])

    def interrupt():
        raise RuntimeError('simulated interruption after draft')

    with pytest.raises(RuntimeError, match='simulated interruption'):
        GraphRunner(engine, fault_hook=interrupt).execute(claim['run_id'], 'TASK-001', manifest)
    # A fresh Python process and connection resume the saved Run, not in-memory state.
    script = ("import json,sys; from app.work.db import engine; "
              "from app.runtime.graph import GraphRunner; "
              "report,metadata=GraphRunner(engine).execute(sys.argv[1],resume=True); "
              "print(json.dumps({'report':report,'metadata':metadata}))")
    result = subprocess.run([sys.executable, '-c', script, claim['run_id']], check=True,
                            cwd=Path(__file__).resolve().parents[1] / 'backend',
                            env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8')
    resumed = json.loads(result.stdout)
    report, metadata = resumed['report'], resumed['metadata']
    kernel.record_market_model(claim['run_id'], metadata)
    assert kernel.submit(claim['run_id'], report)['passed']
    with engine.connect() as conn:
        steps = conn.execute(text('SELECT node_name,status FROM run_steps WHERE run_id=:id ORDER BY step_id'),
                             {'id':claim['run_id']}).all()
        assert steps == [('inspect_context','STARTED'),('inspect_context','SUCCEEDED'),
                         ('draft','STARTED'),('draft','SUCCEEDED'),
                         ('finalize','STARTED'),('finalize','FAILED'),
                         ('finalize','STARTED'),('finalize','SUCCEEDED')]
        assert conn.execute(text('SELECT status FROM work_items WHERE work_id=:id'), {'id':work}).scalar_one() == 'COMMITTED'
    with pytest.raises(ValueError, match='not active'):
        GraphRunner(engine).execute(claim['run_id'], resume=True)


def test_graph_rejects_modified_context_before_drafting(kernel):
    seed(engine)
    _, _ = kernel.trigger('TASK-001-JOB', 'graph-tamper', {'dataset_version':'v1','model_mode':'MOCK'})
    claim = kernel.claim()
    manifest = kernel.prepare_market(claim['run_id'])
    manifest['dataset_version'] = 'v2'
    with pytest.raises(ValueError, match='hash mismatch'):
        GraphRunner(engine).execute(claim['run_id'], 'TASK-001', manifest)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM artifacts WHERE run_id=:id'), {'id':claim['run_id']}).scalar_one() == 0
