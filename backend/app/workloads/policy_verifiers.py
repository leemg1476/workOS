"""Independent deterministic verification for the policy demo and its DAG."""
from pydantic import ValidationError
from sqlalchemy import text
from app.domain.contracts import PolicyFollowupVerification, PolicyIndexReceipt, PolicyRegressionReport
from app.evaluation.policy_regression import load_inputs as load_regression_inputs, make_report as make_regression_report
from app.integrations.search import retrieve_documents, demo_index_target
from app.work.market import digest
from app.work.policy import verify_policy, build_policy_manifest
from app.work.service import Conflict, canonical, one
from app.workloads.registry import Verifier, register_builtin_verifier, register_verifier_kind_resolver

def policy_regression_chain_current(c, work_id, allow_unpinned=False):
    rows = c.execute(text('''WITH RECURSIVE chain AS (
        SELECT d.predecessor_id,d.successor_id,d.pinned_artifact_version,1 AS depth
            FROM work_dependencies d WHERE d.successor_id=:id
        UNION ALL SELECT d.predecessor_id,d.successor_id,d.pinned_artifact_version,chain.depth+1
            FROM work_dependencies d JOIN chain ON d.successor_id=chain.predecessor_id WHERE chain.depth<4)
        SELECT chain.*,p.status,p.accepted_artifact_id FROM chain
            JOIN work_items p ON p.work_id=chain.predecessor_id ORDER BY depth'''), {'id':work_id}).mappings().all()
    return len(rows)==3 and all(row['status']=='COMMITTED' and row['accepted_artifact_id'] is not None
        and ((allow_unpinned and row['depth']==1) or row['pinned_artifact_version']==row['accepted_artifact_id']) for row in rows)


def verify_impact(c, run, work, task, content, artifact_hash):
    run_id = run['run_id']
    context = one(c, 'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id', {'id':run_id})
    if not context:
        raise Conflict('policy context missing')
    manifest = context['manifest']
    result = verify_policy(manifest,content)
    current = build_policy_manifest(c,run_id,run['work_id'])
    result['checks']['manifest_hash'] = digest(manifest) == context['sha256']
    result['checks']['graph_still_matches'] = current['followups'] == manifest['followups']
    result['checks']['previous_version_search_current'] = current['indexed_previous_document'] == manifest['indexed_previous_document']
    result['checks']['execution_provenance'] = run['agent_version'] == 'policy-deterministic-v1' and run['model_id'] == 'MOCK'
    result['passed'] = all(result['checks'].values())
    refs = {'task_id':'TASK-002','task_version':work['task_version'],'artifact_sha256':artifact_hash,
            'context_sha256':context['sha256'],'event_id':manifest['event_id'],'document_id':manifest['document_id'],
            'old_version':manifest['old_document']['version'],'new_version':manifest['new_document']['version']}
    return result, refs

def verify_followup(c, run, work, task, content, artifact_hash):
    run_id = run['run_id']
    context = one(c, 'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id', {'id':run_id})
    if not context:
        raise Conflict('follow-up context missing')
    manifest = context['manifest']
    parent = one(c, '''SELECT w.status,w.accepted_artifact_id,d.pinned_artifact_version FROM work_items w
        JOIN work_dependencies d ON d.predecessor_id=w.work_id
        WHERE w.work_id=:parent AND d.successor_id=:child''',
        {'parent':manifest['parent_work_id'],'child':work['work_id']})
    original = verify_policy(manifest['parent_manifest'],manifest['parent_artifact'])
    expected = {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':1,
        'parent_artifact_id':manifest['parent_artifact_id'],
        'parent_artifact_sha256':manifest['parent_artifact_sha256'],
        'parent_context_sha256':manifest['parent_context_sha256'],
        'verification':'DETERMINISTIC_RECHECK','semantic_review':'NOT_PERFORMED'}
    try:
        PolicyFollowupVerification.model_validate(content)
        schema_valid = True
    except ValidationError:
        schema_valid = False
    checks = {'context_hash':digest(manifest)==context['sha256'],
        'parent_context_hash':digest(manifest['parent_manifest'])==manifest['parent_context_sha256'],
        'parent_artifact_hash':digest(manifest['parent_artifact'])==manifest['parent_artifact_sha256'],
        'parent_evidence_passed':manifest['parent_evidence'].get('passed') is True,
        'parent_recheck':original['passed'],
        'parent_current':bool(parent) and parent['status']=='COMMITTED'
            and str(parent['accepted_artifact_id'])==str(parent['pinned_artifact_version'])==manifest['parent_artifact_id'],
        'schema':schema_valid,'receipt_matches':content==expected,
        'execution_provenance':run['agent_version']=='policy-followup-verification-v1' and run['model_id']=='MOCK'}
    result = {'passed':all(checks.values()),'checks':checks,'semantic_review':'NOT_PERFORMED'}
    refs = {'task_id':'TASK-002','task_version':2,'artifact_sha256':artifact_hash,
            'context_sha256':context['sha256'],'parent_artifact_id':manifest['parent_artifact_id'],
            'parent_context_sha256':manifest['parent_context_sha256']}
    return result, refs

def verify_index(c, run, work, task, content, artifact_hash):
    run_id = run['run_id']
    context = one(c,'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id',{'id':run_id})
    if not context:
        raise Conflict('policy index context missing')
    manifest = context['manifest']
    doc = manifest['document']
    doc_id = f"{doc['document_id']}@{doc['version']}"
    expected_doc = {'id':doc_id,'title':doc['title'],'body':doc['body'],'sha256':doc['sha256']}
    _,current_index = demo_index_target()
    found,retrieval = retrieve_documents([doc_id]+[x['id'] for x in manifest['previous_indexed_document']])
    current_doc = next((item for item in found if item['id']==doc_id),None)
    old_docs = [item for item in found if item['id']!=doc_id]
    source = one(c,'''SELECT title,clauses,sha256 FROM policy_documents
        WHERE document_id=:id AND version=:version''', {'id':doc['document_id'],'version':doc['version']})
    predecessor = one(c,'''SELECT p.status,p.accepted_artifact_id,d.pinned_artifact_version,
        root.status AS root_status,root.accepted_artifact_id AS root_artifact_id,rd.pinned_artifact_version AS root_pin
        FROM work_items p JOIN work_dependencies d ON d.predecessor_id=p.work_id AND d.successor_id=:work
        JOIN work_items root ON root.work_id=:root
        JOIN work_dependencies rd ON rd.predecessor_id=root.work_id AND rd.successor_id=p.work_id
        WHERE p.work_id=:predecessor''',{'work':work['work_id'],'root':manifest['root_work_id'],
                                          'predecessor':manifest['predecessor_work_id']})
    try:
        PolicyIndexReceipt.model_validate(content)
        schema_valid = True
    except ValidationError:
        schema_valid = False
    checks = {'schema':schema_valid,'context_hash':digest(manifest)==context['sha256'],
        'index_target':current_index==manifest['index']==retrieval['index'],
        'indexed_version_and_hash':current_doc==expected_doc,
        'previous_version_preserved':old_docs==manifest['previous_indexed_document'],
        'source_current':bool(source) and source['title']==doc['title'] and canonical(source['clauses'])==doc['body']
            and digest(source['clauses'])==source['sha256']==doc['sha256'],
        'predecessor_current':bool(predecessor) and predecessor['status']=='COMMITTED'
            and str(predecessor['accepted_artifact_id'])==str(predecessor['pinned_artifact_version'])==manifest['predecessor_artifact_id']
            and predecessor['root_status']=='COMMITTED'
            and str(predecessor['root_artifact_id'])==str(predecessor['root_pin'])==manifest['root_artifact_id'],
        'receipt_matches':content.get('task_id')=='TASK-002' and content.get('data_mode')=='DEMO DATA'
            and content.get('step_no')==2 and content.get('predecessor_artifact_id')==manifest['predecessor_artifact_id']
            and content.get('document_id')==doc['document_id'] and content.get('document_version')==doc['version']
            and content.get('document_sha256')==doc['sha256'] and content.get('index')==manifest['index'],
        'execution_provenance':run['agent_version']=='policy-index-v1' and run['model_id']=='MOCK'}
    result = {'passed':all(checks.values()),'checks':checks,'retrieval':retrieval,'indexed_document':current_doc}
    refs = {'task_id':'TASK-002','task_version':2,'artifact_sha256':artifact_hash,'context_sha256':context['sha256'],
            'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':doc_id,
            'document_sha256':doc['sha256'],'index':manifest['index'],'retrieval':retrieval}
    return result, refs

def verify_regression(c, run, work, task, content, artifact_hash):
    run_id = run['run_id']
    context = one(c,'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id',{'id':run_id})
    if not context:
        raise Conflict('policy regression context missing')
    manifest = context['manifest']
    _,index = demo_index_target()
    documents,retrieval = retrieve_documents([manifest['document']['id']])
    current_inputs = load_regression_inputs(c)
    expected = make_regression_report(manifest)
    try:
        PolicyRegressionReport.model_validate(content)
        schema_valid = True
    except ValidationError:
        schema_valid = False
    checks = {'schema':schema_valid,'context_hash':digest(manifest)==context['sha256'],
        'index_current':index==manifest['retrieval']['index']==retrieval['index'] and documents==[manifest['document']],
        'inputs_current':current_inputs==manifest['evaluation_inputs'],
        'predecessor_current':policy_regression_chain_current(c,work['work_id']),
        'report_recomputed':content==expected,
        'zero_classification_regressions':all(not suite['regressions'] for suite in expected['suites']),
        'execution_provenance':run['agent_version']=='policy-regression-v1' and run['model_id']=='MOCK'}
    result = {'passed':all(checks.values()),'checks':checks,'scope':expected['scope'],
              'suite_results':[{'suite_version':suite['suite_version'],'case_count':suite['case_count'],
                'baseline_correct':suite['baseline_correct'],'active_correct':suite['active_correct'],
                'regressions':suite['regressions']} for suite in expected['suites']]}
    refs = {'task_id':'TASK-002','task_version':2,'artifact_sha256':artifact_hash,'context_sha256':context['sha256'],
            'predecessor_artifact_id':manifest['predecessor_artifact_id'],'document_id':manifest['document']['id'],
            'document_sha256':manifest['document']['sha256'],
            'suite_hashes':{s['suite_version']:s['sha256'] for s in manifest['evaluation_inputs']['suites']},
            'baseline_method_sha256':manifest['evaluation_inputs']['baseline']['sha256'],
            'active_method_sha256':manifest['evaluation_inputs']['active']['sha256'],
            'active_method_state_version':manifest['evaluation_inputs']['state']['version']}
    return result, refs

def resolve_policy_verifier(work):
    if work['task_id'] == 'TASK-002' and work['parent_work_id']:
        return {1:'policy_followup_verification_v1',2:'policy_index_v1',3:'policy_regression_v1'}.get(
            work['input_snapshot'].get('step_no'),'policy_impact_v1')
    return 'policy_impact_v1'


register_verifier_kind_resolver('policy_impact_v1',resolve_policy_verifier)
register_builtin_verifier('policy_impact_v1', Verifier('policy_impact','1','policy-validator-v2','policy_deterministic','policy-suite-v2',verify_impact))
register_builtin_verifier('policy_followup_verification_v1', Verifier('policy_followup_verification','1','policy-followup-verifier-v1','policy_followup_recheck','policy-followup-suite-v1',verify_followup))
register_builtin_verifier('policy_index_v1', Verifier('policy_index_receipt','1','policy-index-verifier-v1','policy_index_recheck','policy-index-suite-v1',verify_index))
register_builtin_verifier('policy_regression_v1', Verifier('policy_regression_report','1','policy-regression-verifier-v1','policy_regression_check','policy-voc-regression-v1',verify_regression))
