"""Deterministic evidence checks for versioned VOC reports."""
from sqlalchemy import text
from app.integrations.search import retrieve_documents
from app.integrations.sql_source import source_engine
from app.work.market import digest
from app.work.service import Conflict, one
from app.work.voc import verify_voc, DOCUMENTS as VOC_DOCUMENTS, DOCUMENT_QUERY as VOC_DOCUMENT_QUERY, DOCUMENT_PARAMETERS as VOC_DOCUMENT_PARAMETERS, RECORD_QUERY as VOC_RECORD_QUERY
from app.workloads.registry import Verifier, register_builtin_verifier, register_verifier, register_verifier_kind_resolver

def previous_voc_report(c, artifact_id=None, source_version=None):
    selection = 'AND a.artifact_id=:artifact' if artifact_id else ''
    selection += " AND a.content->>'source_version'=:source" if source_version else ''
    row = one(c, '''SELECT a.artifact_id,a.artifact_version,a.schema_version,a.sha256,a.content,r.run_id,r.finished_at,
        w.work_id,w.task_version,e.evidence_id,e.verifier_version,ev.evaluation_id,ev.eval_suite_version FROM artifacts a JOIN runs r ON r.run_id=a.run_id
        JOIN work_items w ON w.work_id=r.work_id JOIN evidence e ON e.artifact_id=a.artifact_id
        JOIN evaluations ev ON ev.artifact_id=a.artifact_id AND ev.verdict='PASS'
        WHERE a.kind='voc_report' AND r.status='SUCCEEDED' AND e.verification_type='voc_deterministic'
        AND e.result->>'passed'='true' '''+selection+''' ORDER BY a.artifact_version DESC LIMIT 1''',
              {'artifact':artifact_id,'source':source_version})
    if not row:
        return None
    return {'artifact_id':str(row['artifact_id']),'artifact_version':row['artifact_version'],'sha256':row['sha256'],
            'schema_version':row['schema_version'],'content':row['content'],'run_id':str(row['run_id']),'work_id':str(row['work_id']),
            'task_version':row['task_version'],'evidence_id':str(row['evidence_id']),'verifier_version':row['verifier_version'],
            'evaluation_id':str(row['evaluation_id']),'evaluation_suite_version':row['eval_suite_version'],
            'finished_at':row['finished_at'].isoformat()}


def verify(c, run, work, task, content, artifact_hash):
    run_id = run['run_id']
    kind = task['acceptance_policy']['kind']
    context = one(c, 'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id', {'id':run_id})
    if not context:
        raise Conflict('VOC context missing')
    manifest = context['manifest']
    result = verify_voc(manifest,content)
    source = one(c, "SELECT * FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")
    result['checks']['manifest_hash'] = digest(manifest) == context['sha256']
    result['checks']['source_dependency_unchanged'] = (dict(source) if source else None) == manifest['source_dependency']
    result['checks']['execution_provenance'] = run['agent_version'] == manifest['method']['method_id'] and run['model_id'] == 'MOCK'
    if work['input_snapshot'].get('source_version','voc-demo-v1') != 'voc-demo-v1':
        result['checks']['poc_source_pinned'] = manifest['source_version']==work['input_snapshot'].get('source_version')
    if kind in ('voc_report_v2','voc_report_v3'):
        previous = manifest['previous_report']
        result['checks']['previous_artifact_verified'] = (previous is None or
            previous_voc_report(c,previous['artifact_id'])==previous)
    if kind=='voc_report_v3':
        with source_engine(c.engine).connect() as source_conn:
            current_records = [dict(row) for row in source_conn.execute(text(VOC_RECORD_QUERY),{'version':manifest['source_version']}).mappings()]
            current = source_conn.execute(text(VOC_DOCUMENT_QUERY),VOC_DOCUMENT_PARAMETERS).mappings().all()
        current = [{'id':f"{d['document_id']}@{d['version']}",'title':d['title'],'body':d['body'],'sha256':d['sha256']}
                   for d in current if f"{d['document_id']}@{d['version']}" in VOC_DOCUMENTS.values()]
        result['checks']['documents_source_current'] = current==manifest['authoritative_documents']
        result['checks']['records_source_current'] = current_records==manifest['records']
        result['checks']['source_query_contract'] = (manifest['record_query']==VOC_RECORD_QUERY
            and manifest['record_parameters']=={'version':manifest['source_version']} and manifest['document_query']==VOC_DOCUMENT_QUERY
            and manifest['document_parameters']==VOC_DOCUMENT_PARAMETERS)
        if manifest['retrieval']['mode']=='OPENSEARCH':
            indexed,_ = retrieve_documents(list(VOC_DOCUMENTS.values()))
            result['checks']['documents_search_current'] = indexed==manifest['documents']
    result['passed'] = all(result['checks'].values())
    refs = {'task_id':'TASK-003','task_version':work['task_version'],'artifact_sha256':artifact_hash,
            'context_sha256':context['sha256'],'source_version':manifest['source_version'],
            'records_sha256':manifest['records_sha256'],'method_version':manifest['method']['method_id']}
    if kind in ('voc_report_v2','voc_report_v3'):
        refs['previous_artifact_id'] = manifest['previous_report']['artifact_id'] if manifest['previous_report'] else None
        refs['history_as_of'] = manifest['history_as_of']
        refs['comparison_basis'] = 'CATEGORY_ONLY_NOT_SEMANTIC'
    if kind=='voc_report_v3':
        refs.update(document_ids=[d['id'] for d in manifest['documents']],retrieval=manifest['retrieval'],
                    document_query=manifest['document_query'],document_parameters=manifest['document_parameters'])
    return result, refs

register_builtin_verifier('voc_report_v1', Verifier('voc_report','1','voc-validator-v1','voc_deterministic','voc-run-suite-v1',verify))
register_builtin_verifier('voc_report_v2', Verifier('voc_report','2','voc-validator-v2','voc_deterministic','voc-run-suite-v2',verify))
register_builtin_verifier('voc_report_v3', Verifier('voc_report','3','voc-validator-v3','voc_deterministic','voc-run-suite-v3',verify))
register_verifier('voc_poc_v1', Verifier('voc_report','3','voc-poc-validator-v1','voc_deterministic','voc-poc-run-suite-v1',verify))
register_verifier_kind_resolver('voc_report_v3',lambda work: 'voc_poc_v1' if work['input_snapshot'].get('source_version','voc-demo-v1')!='voc-demo-v1' else 'voc_report_v3')
