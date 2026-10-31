"""Redis Streams transports durable-queue wakeups; PostgreSQL remains authoritative."""
import os
from sqlalchemy import text
from redis import Redis
from redis.exceptions import ResponseError

STREAM = 'workos:jobs'
GROUP = 'workos:workers'


def client():
    return Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True, socket_timeout=3)


def ensure_group(redis):
    try:
        redis.xgroup_create(STREAM, GROUP, id='0', mkstream=True)
    except ResponseError as exc:
        if 'BUSYGROUP' not in str(exc):
            raise


def dispatch(db, redis):
    # XADD may succeed before the transaction commits; duplicate wakeups are safe.
    with db.begin() as c:
        pending = c.execute(text("""SELECT work_id FROM work_queue WHERE status='PENDING' AND notified_at IS NULL
            AND available_at <= now() FOR UPDATE SKIP LOCKED LIMIT 100""")).mappings().all()
        for item in pending:
            redis.xadd(STREAM, {'work_id': str(item['work_id'])})
            c.execute(text('UPDATE work_queue SET notified_at=now() WHERE work_id=:id'), {'id': item['work_id']})
        return len(pending)
