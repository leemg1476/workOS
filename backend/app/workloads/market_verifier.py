"""Deterministic evidence checks for the market briefing demo."""
from sqlalchemy import text
from app.integrations.search import retrieve_documents
from app.work.market import digest, verify_briefing
from app.work.service import Conflict, one
from app.workloads.registry import Verifier, register_builtin_verifier


def verify(c,run,work,task,content,artifact_hash):
    context=one(c,'SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id',{'id':run['run_id']})
    if not context:
        raise Conflict('market context missing')
    result=verify_briefing(context['manifest'],content)
    result['checks']['manifest_hash']=digest(context['manifest'])==context['sha256']
    result['checks']['model_provenance']=run['agent_version']=='market-'+content.get('model_mode','')
    current_docs=c.execute(text('SELECT document_id,version,title,body,sha256 FROM source_documents ORDER BY document_id,version')).mappings().all()
    current_docs=[{'id':f"{d['document_id']}@{d['version']}",'title':d['title'],'body':d['body'],'sha256':d['sha256']}
                  for d in current_docs if f"{d['document_id']}@{d['version']}" in context['manifest']['required_document_ids']]
    result['checks']['source_current']=current_docs==context['manifest']['authoritative_documents']
    if context['manifest']['retrieval']['mode']=='OPENSEARCH':
        current_index,_=retrieve_documents(context['manifest']['required_document_ids'])
        result['checks']['search_current']=current_index==context['manifest']['documents']
    result['passed']=all(result['checks'].values())
    refs={'task_id':work['task_id'],'task_version':work['task_version'],'artifact_sha256':artifact_hash,
          'context_sha256':context['sha256'],'dataset_version':context['manifest']['dataset_version'],
          'document_ids':[d['id'] for d in context['manifest']['documents']],'retrieval':context['manifest']['retrieval']}
    return result,refs


register_builtin_verifier('market_briefing_v1',Verifier('market_briefing','1','market-validator-v3',
                          'market_deterministic','market-suite-v3',verify))
