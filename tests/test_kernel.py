from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import text
from app.work.db import engine
from app.work.service import Conflict
from app.domain.contracts import TRANSITIONS, WorkStatus


def rows(query, **params):
    with engine.connect() as c:
        return c.execute(text(query), params).mappings().all()


def trigger(k, key):
    return k.trigger('test-job', key, {'input': key})[0]


def accepted(k, key):
    work = trigger(k, key)
    run = k.claim()
    assert run['work_id'] == work
    result = k.submit(run['run_id'], {'mode': 'MOCK', 'task_id': 'TEST', 'value': key})
    assert result['passed']
    return work, result['artifact_id']


def test_transition_rules(kernel):
    assert WorkStatus.COMMITTED not in TRANSITIONS[WorkStatus.RUNNING]
    work = trigger(kernel, 'transition')
    with engine.begin() as c:
        with pytest.raises(Conflict):
            kernel._transition(c, work, 'COMMITTED')
    assert rows('SELECT status FROM work_items WHERE work_id=:id', id=work)[0]['status'] == 'READY'


def test_duplicate_trigger_and_key_conflict(kernel):
    def fire(_):
        return kernel.trigger('test-job', 'same-key', {'x': 1})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fire, range(6)))
    assert len({x[0] for x in results}) == 1
    assert sum(x[1] for x in results) == 1
    with pytest.raises(Conflict):
        kernel.trigger('test-job', 'same-key', {'x': 2})
    assert len(rows('SELECT * FROM work_items')) == 1


def test_concurrent_claim_and_retry_preserves_failed_run(kernel):
    work = trigger(kernel, 'claim')
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: kernel.claim(), range(6)))
    claims = [c for c in claims if c]
    assert len(claims) == 1
    first = claims[0]
    kernel.fail(first['run_id'], 'fixture failure')
    assert kernel.retry(work, 'retry-one')
    assert not kernel.retry(work, 'retry-one')
    second = kernel.claim()
    assert second['work_id'] == work and second['run_id'] != first['run_id'] and second['attempt_no'] == 2
    assert [r['status'] for r in rows('SELECT status FROM runs ORDER BY attempt_no')] == ['FAILED', 'RUNNING']


def test_lease_recovery_rejects_late_worker(kernel):
    work = trigger(kernel, 'lease')
    first = kernel.claim(lease_seconds=-1)
    with pytest.raises(Conflict):
        kernel.submit(first['run_id'], {'mode':'MOCK','task_id':'TEST','value':'late'})
    second = kernel.claim()
    assert second['work_id'] == work and second['attempt_no'] == 2
    assert rows('SELECT error FROM runs WHERE run_id=:id', id=first['run_id'])[0]['error'] == 'worker lease expired'


def test_dependency_cycle_and_execution_order(kernel):
    a, b, c = [trigger(kernel, x) for x in ('a', 'b', 'c')]
    kernel.add_dependency(a, b)
    kernel.add_dependency(b, c)
    with pytest.raises(Conflict):
        kernel.add_dependency(c, a)
    assert kernel.claim()['work_id'] == a
    assert kernel.claim() is None


def test_failed_verification_preserves_accepted_artifact(kernel):
    work, artifact = accepted(kernel, 'old')
    kernel.invalidate(work, 'source-v2')
    kernel.retry(work, 'recheck')
    run = kernel.claim()
    result = kernel.submit(run['run_id'], {'mode':'MOCK','task_id':'TEST'})
    assert not result['passed']
    state = rows('SELECT status,accepted_artifact_id FROM work_items WHERE work_id=:id', id=work)[0]
    assert state['status'] == 'REWORK_REQUIRED' and str(state['accepted_artifact_id']) == artifact
    assert rows('SELECT result FROM evidence ORDER BY created_at DESC')[0]['result']['passed'] is False
    assert len(rows('SELECT * FROM artifacts')) == 2


def test_stale_propagation_and_repin(kernel):
    a, a_artifact = accepted(kernel, 'first')
    b = trigger(kernel, 'second')
    kernel.add_dependency(a, b)
    b_run = kernel.claim()
    assert b_run['work_id'] == b
    kernel.submit(b_run['run_id'], {'mode':'MOCK','task_id':'TEST','value':'b'})
    other, _ = accepted(kernel, 'unrelated')
    kernel.invalidate(a, 'source-change')
    states = {str(r['work_id']): r['status'] for r in rows('SELECT work_id,status FROM work_items')}
    assert states == {a:'STALE', b:'STALE', other:'COMMITTED'}
    with pytest.raises(Conflict):
        kernel.retry(b, 'too-early')
    kernel.retry(a, 'rerun-a')
    new_run = kernel.claim()
    new_artifact = kernel.submit(new_run['run_id'], {'mode':'MOCK','task_id':'TEST','value':'new'})['artifact_id']
    assert new_artifact != a_artifact
    kernel.retry(b, 'rerun-b')
    pin = rows('SELECT pinned_artifact_version FROM work_dependencies WHERE successor_id=:id', id=b)[0]['pinned_artifact_version']
    assert str(pin) == new_artifact
    assert kernel.claim()['work_id'] == b


def test_event_ledger_is_immutable(kernel):
    trigger(kernel, 'ledger')
    with pytest.raises(Exception):
        with engine.begin() as c:
            c.execute(text("UPDATE work_events SET actor='changed'"))


def test_unimplemented_task_cannot_accept_receipt(kernel):
    with engine.begin() as c:
        c.execute(text("UPDATE task_definitions SET acceptance_policy='{\"kind\":\"not_implemented\"}' WHERE task_id='TEST'"))
    trigger(kernel, 'unimplemented')
    run = kernel.claim()
    with pytest.raises(Conflict, match='not implemented'):
        kernel.submit(run['run_id'], {'mode':'MOCK','task_id':'TEST','value':'receipt'})
    assert not rows('SELECT * FROM artifacts')


def test_dependency_change_cancels_running_successor(kernel):
    parent, _ = accepted(kernel, 'parent-running')
    child = trigger(kernel, 'child-running')
    kernel.add_dependency(parent, child)
    run = kernel.claim()
    assert run['work_id'] == child
    kernel.invalidate(parent, 'changed-source')
    assert rows('SELECT status FROM work_items WHERE work_id=:id', id=child)[0]['status'] == 'REWORK_REQUIRED'
    assert rows('SELECT error FROM runs WHERE run_id=:id', id=run['run_id'])[0]['error'] == 'dependency became stale'
    with pytest.raises(Conflict):
        kernel.submit(run['run_id'], {'mode':'MOCK','task_id':'TEST','value':'late'})
