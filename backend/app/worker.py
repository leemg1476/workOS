import time
import os
from uuid import uuid4
from app.work.db import engine, migrate
from app.work.service import Kernel, Conflict
from app.work.notifications import client, ensure_group, STREAM, GROUP
from app.runtime.graph import GraphRunner
from app.integrations.search import index_demo_policy
from app.evaluation.policy_regression import make_report as make_policy_regression_report
from redis.exceptions import RedisError
from app.workloads.registry import executors, kind_executors
from app.workloads import text_workflow
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
        if claim['task_id'] == 'TASK-002' and claim['input_snapshot'].get('step_no') == 3:
            manifest = kernel.prepare_policy_regression(claim['run_id'])
            return kernel.submit(claim['run_id'],make_policy_regression_report(manifest))
        if claim['task_id'] == 'TASK-002' and claim['input_snapshot'].get('step_no') == 2:
            manifest = kernel.prepare_policy_index(claim['run_id'])
            document = manifest['document']
            operation = index_demo_policy(document,manifest['index'])
            report = {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':2,
                      'predecessor_artifact_id':manifest['predecessor_artifact_id'],
                      'document_id':document['document_id'],'document_version':document['version'],
                      'document_sha256':document['sha256'],'index':manifest['index'],'operation':operation}
            return kernel.submit(claim['run_id'],report)
        if claim['task_id'] == 'TASK-002' and claim['input_snapshot'].get('step_no') == 1:
            manifest = kernel.prepare_policy_verification(claim['run_id'])
            report = {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':1,
                      'parent_artifact_id':manifest['parent_artifact_id'],
                      'parent_artifact_sha256':manifest['parent_artifact_sha256'],
                      'parent_context_sha256':manifest['parent_context_sha256'],
                      'verification':'DETERMINISTIC_RECHECK','semantic_review':'NOT_PERFORMED'}
            return kernel.submit(claim['run_id'],report)
        if claim['task_id'] == 'TASK-003' and claim['task_version'] in (2,3,4):
            manifest = kernel.prepare_voc(claim['run_id'])
            report, _ = GraphRunner(kernel.engine).execute(claim['run_id'], claim['task_id'], manifest)
            return kernel.submit(claim['run_id'], report)
        if claim['task_id'] == 'TASK-002' and claim['task_version'] == 2:
            manifest = kernel.prepare_policy(claim['run_id'])
            report, _ = GraphRunner(kernel.engine).execute(claim['run_id'], claim['task_id'], manifest)
            return kernel.submit(claim['run_id'], report)
        if claim['task_id'] == 'TASK-001' and claim['task_version'] == 2:
            mode = claim['input_snapshot'].get('model_mode', os.getenv('WORKOS_LLM_MODE', 'MOCK'))
            manifest = kernel.prepare_market(claim['run_id'])
            model = (os.getenv('openai_model') or os.getenv('OPENAI_MODEL') or 'NOT_CONFIGURED') if mode == 'LIVE_LLM' else 'MOCK'
            kernel.record_market_model_started(claim['run_id'], mode, model)
            report, metadata = GraphRunner(kernel.engine).execute(claim['run_id'], claim['task_id'], manifest, mode)
            kernel.record_market_model(claim['run_id'], metadata)
            return kernel.submit(claim['run_id'], report)
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
    kernel.recover_policy_followups()
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
