"""Knowledge comparisons reuse frozen text suites and paired Work execution."""
from copy import deepcopy
from sqlalchemy import text
from app.domain.hashing import digest,canonical
from app.work.service import Conflict,NotFound,one
from app.work.procedural_knowledge import binding_for,applicable


def _entry_snapshot(row):
    return {k:(str(row[k]) if k in ('entry_id','task_id') else row[k]) for k in
            ('entry_id','task_id','task_version','kind','title','payload','sha256','source_scope_sha256')}


def base_snapshot(c,task_id,version,source):
    from app.work.knowledge import active_snapshot
    resolved=active_snapshot(c,task_id,version,source)['active']
    ids=[r['entry_id'] for r in resolved]
    rows=[]
    for entry_id in ids:
        original=one(c,'SELECT * FROM task_knowledge WHERE entry_id=CAST(:id AS uuid)',{'id':entry_id})
        rows.append({'entry':_entry_snapshot(original),'resolved':next(r for r in resolved if r['entry_id']==entry_id)})
    return rows


def freeze(c,experiment_id,task,candidate_entry_id):
    from app.work.knowledge import entries,source_text
    candidate=next((r for r in entries(c,task['task_id']) if str(r['entry_id'])==str(candidate_entry_id)),None)
    if not candidate or candidate['kind']!='RULE' or candidate['decision'] not in ('PROPOSED','RETIRE'):
        raise Conflict('select a proposed or retired normalized rule from this task')
    source=source_text(c,task['task_id'],task['version'])
    if not applicable(c,candidate,task['task_id'],task['version'],source)[0]:
        raise Conflict('candidate is not compatible with this procedure contract')
    bound=binding_for(c,task['task_id'],task['version'])
    base=base_snapshot(c,task['task_id'],task['version'],source)
    superseded=[str(r['target_entry_id']) for r in c.execute(text("SELECT target_entry_id FROM knowledge_relations WHERE entry_id=CAST(:id AS uuid) AND kind='SUPERSEDES'"),{'id':candidate_entry_id}).mappings()]
    snapshot={'version':'knowledge-replay-v1','task_id':task['task_id'],'task_version':task['version'],
              'candidate':_entry_snapshot(candidate),'base':base,'base_sha256':digest(base),'replaced_entry_ids':superseded,
              'binding':bound['binding'],'binding_sha256':bound['binding_sha256'],
              'memory_scope':'SAME_FROZEN_BASE_BOTH_SIDES; CANDIDATE_ONLY_IN_CHALLENGER; NO_EPISODES_OR_EXPECTED_ANSWERS'}
    c.execute(text('''INSERT INTO text_experiment_knowledge(experiment_id,candidate_entry_id,snapshot,snapshot_sha256)
        VALUES (:id,CAST(:entry AS uuid),CAST(:snapshot AS jsonb),:sha)'''),
        {'id':experiment_id,'entry':candidate_entry_id,'snapshot':canonical(snapshot),'sha':digest(snapshot)})
    return snapshot


def frozen(c,experiment_id):
    row=one(c,'SELECT * FROM text_experiment_knowledge WHERE experiment_id=:id',{'id':experiment_id})
    if not row:return None
    if digest(row['snapshot'])!=row['snapshot_sha256']:
        raise Conflict('knowledge evaluation snapshot changed')
    return row['snapshot']


def pin_memory(c,work,memory):
    mapping=one(c,'SELECT * FROM text_experiment_work WHERE work_id=:id',{'id':work['work_id']})
    if not mapping:return memory
    snapshot=frozen(c,mapping['experiment_id'])
    if not snapshot:return memory
    from app.evaluation.text_runtime import effective_contract
    from app.work.knowledge import _resolve
    task=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:task AND version=:v',{'task':work['task_id'],'v':work['task_version']})
    contract,_=effective_contract(c,work['work_id'],work['task_id'],work['task_version'],task['output_schema'])
    source=contract['source_text'];selected=[];excluded=[]
    candidates=deepcopy(snapshot['base'])
    if mapping['side']=='CHALLENGER':
        candidates=[r for r in candidates if r['entry']['entry_id'] not in snapshot['replaced_entry_ids']]
        candidates.append({'entry':snapshot['candidate'],'resolved':None})
    for row in candidates:
        item=row['entry']
        if item['kind']=='RULE':
            ok,reason=applicable(c,item,work['task_id'],work['task_version'],source)
        else:ok,reason=item['source_scope_sha256']==digest(source),'FROZEN_FACT_NOT_VALID_FOR_CASE_INPUT'
        if ok:selected.append(row['resolved'] if row['resolved'] is not None else _resolve(c,item,source))
        else:excluded.append({'entry_id':item['entry_id'],'reason':reason})
    result=deepcopy(memory)
    result.update(version='task-memory-v4',procedure_binding={'binding':snapshot['binding'],'binding_sha256':snapshot['binding_sha256']},
        knowledge={'version':'knowledge-replay-v1','active':selected,'excluded':excluded,'sha256':digest(selected)},
        evaluation_knowledge={'experiment_id':str(mapping['experiment_id']),'base_sha256':snapshot['base_sha256'],
            'candidate_sha256':snapshot['candidate']['sha256'],'side':mapping['side'],'scope':snapshot['memory_scope']})
    return result


def require_evaluation(c,entry,evaluation_id,replacing=(),*,rollback=False,allow_synthetic_fixture=False):
    """Fail closed on stale, failed, phrase-only, MOCK or unreviewed comparisons."""
    if not evaluation_id:raise Conflict('normalized procedure requires a passing paired knowledge evaluation')
    exp=one(c,'''SELECT x.*,r.result,r.result_sha256,r.verdict FROM text_experiments x
        JOIN text_experiment_results r USING(experiment_id) WHERE x.experiment_id=:id''',{'id':evaluation_id})
    if not exp or exp['task_id']!=entry['task_id']:raise Conflict('knowledge evaluation not found for this task')
    pin=frozen(c,evaluation_id)
    if not pin or pin['candidate']['entry_id']!=str(entry['entry_id']) or pin['candidate']['sha256']!=entry['sha256']:
        raise Conflict('evaluation does not test this candidate version')
    if digest(exp['result'])!=exp['result_sha256'] or exp['verdict']!='PASS':raise Conflict('knowledge comparison did not pass')
    result=exp['result']
    if result.get('activation_scope')=='SYNTHETIC_EXPERIMENT_ONLY' and not allow_synthetic_fixture:
        raise Conflict('synthetic oracle comparisons cannot activate production knowledge or substitute for human-labeled evaluation')
    needed=('all_executions_valid','same_live_model','zero_regressions','at_least_one_gain','all_cases_pass','knowledge_domain_checks','knowledge_heldout_coverage')
    if not all(result.get('gates',{}).get(k) is True for k in needed):raise Conflict('knowledge comparison gates are incomplete')
    if entry['payload']['verification']['semantic_review']=='HUMAN_REQUIRED':
        from app.evaluation.knowledge_semantics import require_human_review
        require_human_review(c,exp)
    from app.evaluation.text_experiments import _active,_suite,_side_result,_targets
    from app.work.knowledge import source_text
    current=_active(c,entry['task_id'])
    if rollback:
        from app.evaluation.text_regression import verify_suite
        row=one(c,'SELECT * FROM text_regression_suites WHERE suite_id=:id',{'id':exp['suite_id']})
        suite=verify_suite(c,entry['task_id'],row)
        historical=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:task AND version=:v',{'task':entry['task_id'],'v':exp['champion_version']})
        current_contract=historical['output_schema']
    else:
        _suite_row,suite=_suite(c,entry['task_id'],exp['suite_id'])
        current_contract=current['output_schema']
    if not rollback and (current['version']!=exp['champion_version'] or digest(current['output_schema'])!=exp['champion_contract_sha256']):
        raise Conflict('task method changed since the comparison')
    current_base=base_snapshot(c,entry['task_id'],current['version'],source_text(c,entry['task_id'],current['version']))
    original_ids={r['entry']['entry_id'] for r in pin['base']}
    current_base=[r for r in current_base if r['entry']['entry_id']!=str(entry['entry_id']) and
                  not (r['entry']['entry_id'] in replacing and r['entry']['entry_id'] not in original_ids)]
    if not rollback and digest(current_base)!=pin['base_sha256']:raise Conflict('baseline knowledge changed; repeat the comparison')
    targets=_targets(c,exp,suite);recorded={r['case_id']:r for r in result['cases']}
    for case in suite['cases']:
        for side in ('CHAMPION','CHALLENGER'):
            actual=_side_result(c,exp,case,side,current_contract,targets)
            if actual!=recorded.get(case['case_id'],{}).get(side.lower()):raise Conflict('evaluation outputs changed')
    return result
