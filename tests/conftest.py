import os
import sys
from pathlib import Path
import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
if not os.environ.get('WORKOS_TEST_DATABASE_URL'):
    raise RuntimeError('Set WORKOS_TEST_DATABASE_URL to a disposable PostgreSQL database; tests delete its data')
os.environ['DATABASE_URL'] = os.environ['WORKOS_TEST_DATABASE_URL']
from app.work.db import engine, migrate
from app.work.service import Kernel


@pytest.fixture
def kernel():
    # Multiple pytest processes may share a disposable DB; never truncate one another's fixtures.
    lock = engine.connect()
    lock.execute(text('SELECT pg_advisory_lock(947299)'))
    try:
        migrate()
        with engine.begin() as c:
            c.execute(text('TRUNCATE voc_records, voc_method_versions, voc_eval_suites, policy_documents, market_observations, source_documents, job_schedule_cursors, work_queue, work_dependencies, evidence, artifacts, runs, work_events, work_items, job_definitions, task_definitions CASCADE'))
            c.execute(text('''INSERT INTO task_definitions VALUES ('TEST',1,'test','test','{}','{}',
              '{"kind":"mock_receipt","required_fields":["mode","task_id","value"]}',true)'''))
            c.execute(text("INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type) VALUES ('test-job','TEST',1,'MANUAL')"))
        yield Kernel(engine)
    finally:
        lock.execute(text('SELECT pg_advisory_unlock(947299)'))
        lock.close()
