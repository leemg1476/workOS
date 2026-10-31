import os
from uuid import uuid4
import pytest
from redis import Redis
from app.work.db import engine
from app.work import notifications


def test_stream_dispatch_and_duplicate_safe_claim(kernel, monkeypatch):
    url = os.getenv('WORKOS_TEST_REDIS_URL')
    if not url:
        pytest.skip('Set WORKOS_TEST_REDIS_URL to run Redis Streams integration')
    redis = Redis.from_url(url, decode_responses=True)
    stream = 'workos:test:' + str(uuid4())
    monkeypatch.setattr(notifications, 'STREAM', stream)
    work, _ = kernel.trigger('test-job', 'redis-test', {})
    try:
        notifications.ensure_group(redis)
        assert notifications.dispatch(engine, redis) == 1
        assert notifications.dispatch(engine, redis) == 0
        messages = redis.xreadgroup(notifications.GROUP, 'test-worker', {stream:'>'}, count=1)
        assert messages[0][1][0][1]['work_id'] == work
        assert kernel.claim()['work_id'] == work
        assert kernel.claim() is None
        assert redis.xack(stream, notifications.GROUP, messages[0][1][0][0]) == 1
    finally:
        redis.delete(stream)
