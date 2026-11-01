"""DB-backed cron cursor: trigger creation is idempotent across crashes and schedulers."""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from croniter import croniter
from sqlalchemy import text
from app.work.db import engine, migrate
from app.work.seed import seed
from app.work.service import Kernel, NotFound
from app.domain.hashing import digest
from app.work.notifications import client, dispatch
from redis.exceptions import RedisError


def next_due(schedule, zone, start):
    local = start.astimezone(ZoneInfo(zone))
    return croniter(schedule, local).get_next(datetime).astimezone(timezone.utc)


def tick(db, now=None):
    now = now or datetime.now(timezone.utc)
    with db.begin() as c:
        jobs = c.execute(text("SELECT * FROM job_definitions WHERE enabled AND trigger_type='CRON' AND schedule IS NOT NULL")).mappings().all()
        for job in jobs:
            c.execute(text('''INSERT INTO job_schedule_cursors(job_id,next_due_at) VALUES (:job,:due)
                ON CONFLICT(job_id) DO NOTHING'''), {'job': job['job_id'], 'due': next_due(job['schedule'], job['timezone'], now)})
        due = c.execute(text('''SELECT j.*,s.next_due_at,p.snapshot AS trigger_preset,p.snapshot_sha256 AS preset_sha256
            FROM job_definitions j JOIN job_schedule_cursors s USING(job_id) LEFT JOIN job_trigger_presets p USING(job_id)
            WHERE j.enabled AND j.trigger_type='CRON' AND s.next_due_at <= :now ORDER BY s.next_due_at LIMIT 100'''), {'now': now}).mappings().all()
    count = 0
    for job in due:
        fire = job['next_due_at'].astimezone(timezone.utc)
        key = f"cron:{job['job_id']}:{fire.isoformat()}"
        preset=job['trigger_preset']
        if preset is not None and (not isinstance(preset,dict) or 'scheduled_for' in preset or digest(preset)!=job['preset_sha256']):
            print(f"Invalid trigger preset for job {job['job_id']}; retained for inspection",flush=True)
            continue
        snapshot={**(preset or {}),'scheduled_for':fire.isoformat()}
        try:
            _, created = Kernel(db).trigger(job['job_id'], key, snapshot, expected_due=fire)
        except NotFound:
            continue  # The job was disabled after the due-job snapshot was read.
        with db.begin() as c:
            c.execute(text('UPDATE job_schedule_cursors SET next_due_at=:next WHERE job_id=:job AND next_due_at=:old'),
                      {'next': next_due(job['schedule'], job['timezone'], fire), 'job': job['job_id'], 'old': fire})
        count += int(created)
    return count


def main():
    migrate()
    seed(engine)
    redis = client()
    while True:
        tick(engine)
        try:
            dispatch(engine, redis)
        except RedisError as exc:
            print(f'Redis dispatch unavailable; PostgreSQL queue retained: {exc}', flush=True)
        time.sleep(1)


if __name__ == '__main__':
    main()
