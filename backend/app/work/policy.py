"""Versioned fictional policy inputs, deterministic change analysis and DAG checks."""
from pydantic import ValidationError
from sqlalchemy import text
from app.domain.contracts import PolicyImpact
from app.work.market import digest
from app.work.market import canonical
from app.integrations.search import configuration, retrieve_documents

STEPS = ['변경 해석 검증', '검색 인덱스 갱신', '회귀 테스트', '담당자 검토']


def make_policy_report(manifest):
    old, new = manifest['old_document'], manifest['new_document']
    changes = []
    for key in sorted(set(old['clauses']) | set(new['clauses'])):
        before, after = old['clauses'].get(key), new['clauses'].get(key)
        if before != after:
            refs = []
            if before is not None:
                refs.append(f"{manifest['document_id']}@{old['version']}#{key}")
            if after is not None:
                refs.append(f"{manifest['document_id']}@{new['version']}#{key}")
            changes.append({'clause_id':key,'before':before,'after':after,'citations':refs})
    return {'task_id':'TASK-002','data_mode':'DEMO DATA','model_mode':'MOCK',
            'document_id':manifest['document_id'],'old_version':old['version'],'new_version':new['version'],
            'changes':changes,'affected_tasks':manifest['affected_tasks'],
            'followups':manifest['followups'],'review_status':'REVIEW_REQUIRED'}


def build_policy_manifest(conn, run_id, work_id):
    event = conn.execute(text('SELECT * FROM policy_change_events WHERE root_work_id=:work'), {'work':work_id}).mappings().first()
    if not event:
        raise ValueError('policy change event not found')
    documents = conn.execute(text('''SELECT document_id,version,title,clauses,sha256 FROM policy_documents
        WHERE document_id=:doc AND version IN (:old,:new) ORDER BY version'''),
        {'doc':event['document_id'],'old':event['old_version'],'new':event['new_version']}).mappings().all()
    by_version = {d['version']:dict(d) for d in documents}
    if event['old_version'] not in by_version or event['new_version'] not in by_version:
        raise ValueError('policy version missing')
    followups = conn.execute(text('''SELECT f.work_id,f.step_no,f.title,d.predecessor_id FROM policy_followups f
        LEFT JOIN work_dependencies d ON d.successor_id=f.work_id WHERE f.root_work_id=:root ORDER BY f.step_no'''),
        {'root':work_id}).mappings().all()
    planned = [{'work_id':str(f['work_id']), 'step_no':f['step_no'], 'title':f['title'],
                'depends_on':str(f['predecessor_id']) if f['predecessor_id'] else None} for f in followups]
    previous = by_version[event['old_version']]
    previous_id = f"{event['document_id']}@{event['old_version']}"
    indexed_source = {'id':previous_id,'title':previous['title'],'body':canonical(previous['clauses']),'sha256':previous['sha256']}
    mode,_,_ = configuration()
    if mode == 'OPENSEARCH':
        indexed,retrieval = retrieve_documents([previous_id])
    else:
        indexed,retrieval = [indexed_source],{'mode':'FIXTURE_DB','document_ids':[previous_id]}
    return {'run_id':str(run_id),'work_id':str(work_id),'task_id':'TASK-002','task_version':2,
            'event_id':event['event_id'],'document_id':event['document_id'],
            'old_document':by_version[event['old_version']],'new_document':by_version[event['new_version']],
            'event_hashes':{'old':event['old_sha256'],'new':event['new_sha256']},
            'affected_tasks':event['affected_tasks'],'followups':planned,'data_mode':'DEMO DATA',
            'indexed_previous_document':indexed,'indexed_previous_sha256':digest(indexed),'retrieval':retrieval,
            'graph_version':'policy-diff-dag-v1','model_id':'MOCK','assumptions':['가상 내부 규정이며 법규 해석이 아님']}


def verify_policy(manifest, report):
    checks = {}
    try:
        parsed = PolicyImpact.model_validate(report)
        checks['schema'] = True
    except ValidationError:
        parsed = None
        checks['schema'] = False
    old,new = manifest['old_document'],manifest['new_document']
    checks['document_versions_and_hashes'] = (old['document_id'] == new['document_id'] == manifest['document_id']
        and old['version'] != new['version'] and digest(old['clauses']) == old['sha256'] == manifest['event_hashes']['old']
        and digest(new['clauses']) == new['sha256'] == manifest['event_hashes']['new'])
    checks['previous_version_retrieval'] = (manifest['indexed_previous_sha256'] == digest(manifest['indexed_previous_document'])
        and manifest['indexed_previous_document'] == [{'id':f"{manifest['document_id']}@{old['version']}",
            'title':old['title'],'body':canonical(old['clauses']),'sha256':old['sha256']}])
    expected_keys = {k for k in set(old['clauses']) | set(new['clauses']) if old['clauses'].get(k) != new['clauses'].get(k)}
    if parsed:
        checks['clause_diff_and_locations'] = (len(parsed.changes) == len(expected_keys)
            and {x.clause_id for x in parsed.changes} == expected_keys
            and all(x.before == old['clauses'].get(x.clause_id) and x.after == new['clauses'].get(x.clause_id)
                    and x.citations == ([f"{manifest['document_id']}@{old['version']}#{x.clause_id}"] if x.before is not None else [])
                    + ([f"{manifest['document_id']}@{new['version']}#{x.clause_id}"] if x.after is not None else [])
                    for x in parsed.changes))
        checks['affected_tasks'] = parsed.affected_tasks == manifest['affected_tasks'] and len(parsed.affected_tasks) > 0
        checks['labels'] = (parsed.task_id == 'TASK-002' and parsed.data_mode == 'DEMO DATA' and parsed.model_mode == 'MOCK'
            and parsed.review_status == 'REVIEW_REQUIRED' and parsed.document_id == manifest['document_id']
            and parsed.old_version == old['version'] and parsed.new_version == new['version'])
        checks['reported_followups'] = parsed.followups == manifest['followups']
    else:
        checks.update(clause_diff_and_locations=False, affected_tasks=False, labels=False, reported_followups=False)
    followups = manifest['followups']
    ids = [x['work_id'] for x in followups]
    checks['acyclic_followup_graph'] = (len(ids) == len(STEPS) and len(set(ids)) == len(ids)
        and [x['title'] for x in followups] == STEPS and [x['step_no'] for x in followups] == list(range(1,len(STEPS)+1))
        and [x['depends_on'] for x in followups] == [manifest['work_id']] + ids[:-1] and manifest['work_id'] not in ids)
    return {'passed':all(checks.values()),'checks':checks,'semantic_interpretation':'NOT_APPLICABLE_DETERMINISTIC_DIFF'}
