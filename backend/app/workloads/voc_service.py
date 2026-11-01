"""VOC demo input and provenance assembly."""
from sqlalchemy import text
from app.integrations.search import retrieve_documents, configuration as search_configuration
from app.integrations.sql_source import source_engine, source_mode
from app.work.market import digest
from app.work.service import Conflict, canonical, one
from app.work.voc import DOCUMENTS as VOC_DOCUMENTS, DOCUMENT_QUERY as VOC_DOCUMENT_QUERY, DOCUMENT_PARAMETERS as VOC_DOCUMENT_PARAMETERS, RECORD_QUERY as VOC_RECORD_QUERY
from app.workloads.voc_verifier import previous_voc_report

def prepare_voc(kernel, run_id):
    with kernel.engine.begin() as c:
        kernel._lock(c)
        run = one(c, """SELECT r.*,w.task_version FROM runs r JOIN work_items w ON w.work_id=r.work_id
            JOIN work_queue q ON q.run_id=r.run_id WHERE r.run_id=:id AND r.status='RUNNING'
            AND q.status='CLAIMED' AND q.lease_until>now() AND w.task_id='TASK-003' AND w.task_version IN (2,3,4)""", {'id':run_id})
        if not run:
            raise Conflict('run is not an eligible VOC task')
        method = one(c, '''SELECT m.* FROM voc_method_state s JOIN voc_method_versions m ON m.method_id=s.active_method_id
            WHERE s.task_id='TASK-003' ''')
        with source_engine(kernel.engine).connect() as source_conn:
            records = [dict(row) for row in source_conn.execute(text(VOC_RECORD_QUERY),{'version':'voc-demo-v1'}).mappings()]
        source = one(c, "SELECT * FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")
        if not method or not records:
            raise Conflict('VOC inputs or method not configured')
        manifest = {'run_id':str(run_id),'work_id':str(run['work_id']),'task_id':'TASK-003','task_version':run['task_version'],
                    'data_mode':'DEMO DATA','source_version':'voc-demo-v1','records':records,'records_sha256':digest(records),'sql_source_mode':source_mode(),
                    'method':dict(method),'source_review_status':source['status'] if source else 'NOT_REGISTERED',
                    'source_dependency':dict(source) if source else None,'model_id':'MOCK','graph_version':'voc-rules-v1'}
        if run['task_version']>=3:
            previous = previous_voc_report(c)
            if previous and digest(previous['content'])!=previous['sha256']:
                raise Conflict('previous VOC artifact hash mismatch')
            manifest.update(previous_report=previous,history_as_of=c.execute(text('SELECT clock_timestamp()')).scalar_one().isoformat(),
                            graph_version='voc-history-v2')
        if run['task_version']>=4:
            with source_engine(kernel.engine).connect() as source_conn:
                docs = source_conn.execute(text(VOC_DOCUMENT_QUERY),VOC_DOCUMENT_PARAMETERS).mappings().all()
            authoritative = [{'id':f"{d['document_id']}@{d['version']}",'title':d['title'],'body':d['body'],'sha256':d['sha256']}
                             for d in docs if f"{d['document_id']}@{d['version']}" in VOC_DOCUMENTS.values()]
            mode,_,_ = search_configuration()
            if mode=='OPENSEARCH':
                documents,retrieval = retrieve_documents(list(VOC_DOCUMENTS.values()))
            else:
                documents = list(authoritative)
                retrieval = {'mode':'FIXTURE_DB','query':'PostgreSQL source_documents by document/version',
                             'document_ids':list(VOC_DOCUMENTS.values())}
            manifest.update(documents=documents,authoritative_documents=authoritative,retrieval=retrieval,
                            retrieval_result_sha256=digest(documents),graph_version='voc-sources-v3',
                            record_query=VOC_RECORD_QUERY,record_parameters={'version':'voc-demo-v1'},
                            document_query=VOC_DOCUMENT_QUERY,document_parameters=VOC_DOCUMENT_PARAMETERS)
        c.execute(text('INSERT INTO run_contexts(run_id,manifest,sha256) VALUES (:id,CAST(:manifest AS jsonb),:sha)'),
                  {'id':run_id,'manifest':canonical(manifest),'sha':digest(manifest)})
        c.execute(text("UPDATE runs SET agent_version=:method,model_id='MOCK',prompt_version='none' WHERE run_id=:id"),
                  {'id':run_id,'method':method['method_id']})
        kernel._event(c,'run.context_created',run_id,{'manifest_sha256':digest(manifest),'method_version':method['method_id']})
        return manifest


