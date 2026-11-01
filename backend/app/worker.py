import time
import os
from uuid import uuid4
from app.work.db import engine, migrate
from app.work.service import Kernel, Conflict
from app.work.notifications import client, ensure_group, STREAM, GROUP
from redis.exceptions import RedisError
from app.workloads.registry import executors, kind_executors
from app.workloads import text_workflow, demo_execution, policy_lifecycle  # Register built-in executors.
from sqlalchemy import text


def process_claim(kernel, claim):
    try:
        executor = executors.get((claim['task_id'], claim['task_version']))
        if executor is not None:
            return kernel.submit(claim['run_id'], executor(claim))
        with kernel.engine.connect() as c:
            task = c.execute(text('SELECT acceptance_policy,output_schema FROM task_definitions WHERE task_id=:id AND version=:version'),
                {'id':claim['task_id'],'version':claim['task_version']}).mappings().one()
        kind_executor = kind_executors.get(task['acceptance_policy'].get('kind'))
        if kind_executor is not None:
            return kernel.submit(claim['run_id'], kind_executor(kernel,claim,task['output_schema']))
        kernel.fail(claim['run_id'], 'MOCK: task workload and acceptance validator are not implemented')
    except Conflict:
        try:
            kernel.fail(claim['run_id'], 'workload conflict: Conflict')
        except Conflict:
            pass  # Expired leases or stale dependencies are already recorded by the kernel.
    except Exception as exc:
        # Do not persist provider messages or request data, which can contain credentials.
        try:
            kernel.fail(claim['run_id'], f'workload execution failed: {type(exc).__name__}')
        except Conflict:
            pass


def main():
    from app.workloads.registry import load_configured
    load_configured()
    migrate()
    kernel = Kernel(engine)
    policy_lifecycle.recover_policy_followups(kernel)
    redis = client()
    consumer = f"{os.getpid()}-{uuid4()}"
    while True:
        messages = []
        try:
            ensure_group(redis)
            messages = redis.xreadgroup(GROUP, consumer, {STREAM: '>'}, count=10, block=1000)
        except RedisError as exc:
            print(f'Redis unavailable; using PostgreSQL recovery polling: {exc}', flush=True)
            time.sleep(1)
        claim = kernel.claim()
        if claim:
            process_claim(kernel, claim)
        else:
            pass
        for _, records in messages:
            for message_id, _ in records:
                try:
                    redis.xack(STREAM, GROUP, message_id)
                except RedisError:
                    pass


if __name__ == '__main__':
    main()
