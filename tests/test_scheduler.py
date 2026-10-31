from datetime import datetime, timezone
from sqlalchemy import text
from app.scheduler import tick
from app.work.db import engine


def test_cron_cursor_recovers_duplicate_fire(kernel):
    with engine.begin() as c:
        c.execute(text("UPDATE job_definitions SET trigger_type='CRON',schedule='0 8 * * 1-5' WHERE job_id='test-job'"))
    start = datetime(2026, 10, 26, 22, 59, tzinfo=timezone.utc)
    fire = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)
    assert tick(engine, start) == 0
    assert tick(engine, fire) == 1
    # Simulate a crash after trigger creation and before cursor advancement.
    with engine.begin() as c:
        c.execute(text("UPDATE job_schedule_cursors SET next_due_at=:fire WHERE job_id='test-job'"), {'fire': fire})
    assert tick(engine, fire) == 0
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM work_items')).scalar_one() == 1
        assert c.execute(text("SELECT next_due_at FROM job_schedule_cursors WHERE job_id='test-job'")).scalar_one() > fire
