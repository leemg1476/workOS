from app.workloads import market_service, policy_service, voc_service
"""Executors for the three explicitly configured demonstration workloads."""
import os
from app.evaluation.policy_regression import make_report as make_policy_regression_report
from app.integrations.search import index_demo_policy
from app.runtime.graph import GraphRunner
from app.workloads.registry import register_kind_executor


def market(kernel, claim, contract):
    if claim['task_id'] != 'TASK-001' or claim['task_version'] != 2:
        raise ValueError('unsupported market task version')
    mode=claim['input_snapshot'].get('model_mode',os.getenv('WORKOS_LLM_MODE','MOCK'))
    manifest=market_service.prepare_market(kernel,claim['run_id'])
    model=(os.getenv('openai_model') or os.getenv('OPENAI_MODEL') or 'NOT_CONFIGURED') if mode=='LIVE_LLM' else 'MOCK'
    market_service.record_market_model_started(kernel,claim['run_id'],mode,model)
    report,metadata=GraphRunner(kernel.engine).execute(claim['run_id'],claim['task_id'],manifest,mode)
    market_service.record_market_model(kernel,claim['run_id'],metadata)
    return report


def policy(kernel, claim, contract):
    if claim['task_id'] != 'TASK-002' or claim['task_version'] != 2:
        raise ValueError('unsupported policy task version')
    step=claim['input_snapshot'].get('step_no')
    if step == 3:
        return make_policy_regression_report(policy_service.prepare_policy_regression(kernel,claim['run_id']))
    if step == 2:
        manifest=policy_service.prepare_policy_index(kernel,claim['run_id']);document=manifest['document']
        operation=index_demo_policy(document,manifest['index'])
        return {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':2,
                'predecessor_artifact_id':manifest['predecessor_artifact_id'],
                'document_id':document['document_id'],'document_version':document['version'],
                'document_sha256':document['sha256'],'index':manifest['index'],'operation':operation}
    if step == 1:
        manifest=policy_service.prepare_policy_verification(kernel,claim['run_id'])
        return {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':1,
                'parent_artifact_id':manifest['parent_artifact_id'],
                'parent_artifact_sha256':manifest['parent_artifact_sha256'],
                'parent_context_sha256':manifest['parent_context_sha256'],
                'verification':'DETERMINISTIC_RECHECK','semantic_review':'NOT_PERFORMED'}
    manifest=policy_service.prepare_policy(kernel,claim['run_id'])
    report,_=GraphRunner(kernel.engine).execute(claim['run_id'],claim['task_id'],manifest)
    return report


def voc(kernel, claim, contract):
    if claim['task_id'] != 'TASK-003' or claim['task_version'] not in (2,3,4):
        raise ValueError('unsupported VOC task version')
    manifest=voc_service.prepare_voc(kernel,claim['run_id'])
    report,_=GraphRunner(kernel.engine).execute(claim['run_id'],claim['task_id'],manifest)
    return report


register_kind_executor('market_briefing_v1',market)
register_kind_executor('policy_impact_v1',policy)
for kind in ('voc_report_v1','voc_report_v2','voc_report_v3'):
    register_kind_executor(kind,voc)
