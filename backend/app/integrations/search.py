"""Read-only OpenSearch retrieval and explicit demo-index initialization."""
import os
import json
import time
from urllib.parse import quote, urlsplit
import httpx
from sqlalchemy import text

DEFAULT_INDEX = 'workos-demo-documents-v1'
DEMO_DOCUMENT_IDS = ('DOC-FX@1', 'DOC-RISK@1', 'POLICY-VOC@1')


class SearchUnavailable(RuntimeError):
    pass


class IndexConflict(RuntimeError):
    pass


def configuration():
    mode = os.getenv('WORKOS_SEARCH_MODE','FIXTURE_DB')
    if mode not in ('FIXTURE_DB','OPENSEARCH'):
        raise ValueError('unsupported search mode')
    return mode, os.getenv('OPENSEARCH_URL','http://localhost:9200').rstrip('/'), os.getenv('OPENSEARCH_INDEX',DEFAULT_INDEX)


def retrieve_documents(ids):
    mode,base,index = configuration()
    if mode != 'OPENSEARCH':
        raise ValueError('OpenSearch retrieval requires OPENSEARCH mode')
    query = {'size':max(len(ids),1),'query':{'ids':{'values':ids}},'sort':['_doc']}
    try:
        response = httpx.post(f'{base}/{quote(index,safe="")}/_search',json=query,timeout=3)
        response.raise_for_status()
        hits = response.json()['hits']['hits']
        documents = sorted([{'id':hit['_id'],'title':hit['_source']['title'],'body':hit['_source']['body'],
                             'sha256':hit['_source']['sha256']} for hit in hits],key=lambda x:x['id'])
    except (httpx.HTTPError,KeyError,ValueError,TypeError) as exc:
        raise SearchUnavailable('OpenSearch retrieval failed') from exc
    return documents, {'mode':'OPENSEARCH','index':index,'query':query,'document_ids':ids}


def demo_index_target():
    """Only the explicitly local Work OS demo index may be modified by the worker."""
    mode,base,index = configuration()
    url = urlsplit(base)
    if (os.getenv('WORKOS_MODE','MOCK').upper() != 'MOCK' or mode != 'OPENSEARCH'
            or url.scheme != 'http' or url.hostname not in ('localhost','127.0.0.1','::1','opensearch')
            or url.username or url.password or url.path not in ('','/') or url.query or url.fragment
            or not (index.startswith('workos-demo-') or index.startswith('workos-test-'))):
        raise SearchUnavailable('local demo OpenSearch index is not configured')
    return base,index


def index_demo_policy(document, expected_index):
    """Create a versioned fixture document once; never overwrite a conflicting version."""
    base,index = demo_index_target()
    if index != expected_index or document['document_id'] != 'POLICY-VOC':
        raise IndexConflict('unexpected demo index or document')
    document_id = f"{document['document_id']}@{document['version']}"
    url = f'{base}/{quote(index,safe="")}/_doc/{quote(document_id,safe="")}'
    try:
        with httpx.Client(timeout=8) as client:
            response = client.put(url,params={'op_type':'create','refresh':'wait_for'},json=document)
            if response.status_code == 409:
                existing = client.get(url)
                existing.raise_for_status()
                if existing.json().get('_source') != document:
                    raise IndexConflict('indexed version differs from source')
                return 'ALREADY_PRESENT'
            response.raise_for_status()
            return 'CREATED'
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
        raise SearchUnavailable('demo policy indexing failed') from exc


def seed_index(db, base=None, index=None):
    _,configured_base,configured_index = configuration()
    base,index = (base or configured_base).rstrip('/'), index or configured_index
    url=f'{base}/{quote(index,safe="")}'
    with httpx.Client(timeout=5) as client:
        exists=client.get(url)
        if exists.status_code == 404:
            created=client.put(url,json={'settings':{'number_of_shards':1,'number_of_replicas':0},
                'mappings':{'properties':{'document_id':{'type':'keyword'},'version':{'type':'integer'},
                                          'title':{'type':'text'},'body':{'type':'text'},'sha256':{'type':'keyword'}}}})
            if created.status_code not in (200,201):
                created.raise_for_status()
        else:
            exists.raise_for_status()
        with db.connect() as conn:
            rows=conn.execute(text('SELECT document_id,version,title,body,sha256 FROM source_documents ORDER BY document_id,version')).mappings().all()
            policies=conn.execute(text('SELECT document_id,version,title,clauses,sha256 FROM policy_documents WHERE version=1 ORDER BY document_id')).mappings().all()
        rows=[dict(row) for row in rows]
        rows.extend({'document_id':row['document_id'],'version':row['version'],'title':row['title'],
                     'body':json.dumps(row['clauses'],sort_keys=True,ensure_ascii=False,separators=(',',':')),'sha256':row['sha256']} for row in policies)
        if not rows:
            raise ValueError('no demo documents available for indexing')
        for row in rows:
            doc=dict(row)
            response=client.put(f"{url}/_doc/{quote(str(doc['document_id'])+'@'+str(doc['version']),safe='')}?refresh=wait_for",json=doc)
            response.raise_for_status()
    return len(rows)


def readiness():
    mode,base,index=configuration()
    if mode == 'FIXTURE_DB':
        return {'mode':mode,'status':'not configured'}
    try:
        documents, _ = retrieve_documents(DEMO_DOCUMENT_IDS)
        ready = {item['id'] for item in documents} == set(DEMO_DOCUMENT_IDS)
        return {'mode':mode,'status':'connected' if ready else 'indexing','index':index}
    except SearchUnavailable:
        return {'mode':mode,'status':'unavailable','index':index}


def seed_main():
    from app.work.db import engine, migrate
    from app.work.seed import seed
    migrate()
    seed(engine)
    for _ in range(60):
        try:
            count=seed_index(engine)
            print(f'DEMO DATA: indexed {count} fictional source documents',flush=True)
            return
        except (httpx.HTTPError,ValueError):
            time.sleep(2)
    raise RuntimeError('OpenSearch demo index initialization timed out')


if __name__ == '__main__':
    seed_main()
