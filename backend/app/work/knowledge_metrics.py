"""Observed compliance and paired utility, with explicit denominators and N/A."""
from itertools import combinations
from uuid import UUID
from pydantic import Field
from sqlalchemy import text
from app.domain.experience import Contract
from app.domain.hashing import digest,canonical
from app.work.service import Kernel,Conflict,NotFound,one,uid


class ApplicabilityLabel(Contract):
    step_id:str=Field(min_length=1,max_length=100)
    expected_rule_ids:list[UUID]=Field(default_factory=list,max_length=100)
    note:str=Field(min_length=1,max_length=2000)
    idempotency_key:str=Field(min_length=1,max_length=150)


def label(engine,run_id,body):
    kernel=Kernel(engine);key='memory.applicability-label:'+body.idempotency_key
    request={'run_id':str(run_id),**body.model_dump(mode='json')}
    with engine.begin() as c:
        kernel._lock(c)
        prior=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if prior:
            if prior['payload']['request']!=request:raise Conflict('label idempotency conflict')
            return prior['payload']['label_id']
        run=one(c,'SELECT r.*,w.task_id,w.task_version FROM runs r JOIN work_items w USING(work_id) WHERE r.run_id=:id',{'id':run_id})
        if not run:raise NotFound('run not found')
        selected=one(c,"SELECT snapshot FROM memory_application_traces WHERE run_id=:id AND step_id=:step AND phase='SELECT'",{'id':run_id,'step':body.step_id})
        if not selected:raise Conflict('a recorded step selection is required')
        ids=[str(x) for x in body.expected_rule_ids]
        if len(set(ids))!=len(ids):raise Conflict('duplicate expected rule')
        # Labels may identify a retrieval miss, so they are not restricted to the
        # candidate set reported by the selector being evaluated.
        for entry_id in ids:
            entry=one(c,'SELECT * FROM task_knowledge WHERE entry_id=CAST(:id AS uuid)',{'id':entry_id})
            if not entry or entry['kind']!='RULE':raise Conflict('expected item is not a normalized rule')
            if entry['task_id']!=run['task_id']:
                from app.work.procedural_knowledge import applicable
                context=one(c,'SELECT manifest FROM run_contexts WHERE run_id=:id',{'id':run_id})
                source=context['manifest']['contract']['source_text']
                if not applicable(c,entry,run['task_id'],run['task_version'],source)[0]:raise Conflict('unrelated rule cannot label this task')
            decision=one(c,'''SELECT decision FROM task_knowledge_decisions WHERE entry_id=CAST(:id AS uuid)
                AND created_at<=:started ORDER BY created_at DESC,decision_id DESC LIMIT 1''',{'id':entry_id,'started':run['started_at']})
            if not decision or decision['decision']!='APPLY':raise Conflict('expected rule was not approved when this Run started')
        snapshot={'request':request,'scope':'EXPLICIT_USER_APPLICABILITY_LABEL_NOT_MODEL_SELF_REPORT'}
        label_id=uid()
        c.execute(text('''INSERT INTO memory_applicability_labels(label_id,run_id,step_id,snapshot,snapshot_sha256)
            VALUES (:id,:run,:step,CAST(:snapshot AS jsonb),:sha)'''),
            {'id':label_id,'run':run_id,'step':body.step_id,'snapshot':canonical(snapshot),'sha':digest(snapshot)})
        kernel._event(c,'memory.applicability_labeled',str(run_id),{'request':request,'label_id':label_id},actor='user',key=key)
        return label_id


def ratio(numerator,denominator,scope):
    return {'value':numerator/denominator if denominator else None,'numerator':numerator,
            'denominator':denominator,'scope':scope,'unavailable_reason':None if denominator else 'NO_ELIGIBLE_OBSERVATIONS'}


def overview(c,task_id):
    from app.work.knowledge import entries
    rows=entries(c,task_id);stats={str(r['entry_id']):{'selected':0,'verified':0,'complied':0,'failed':0,'pending':0} for r in rows}
    traces=c.execute(text('''SELECT t.*,r.status AS run_status,m.manifest FROM memory_application_traces t
        JOIN runs r USING(run_id) JOIN work_items w USING(work_id) JOIN run_task_memories m USING(run_id)
        WHERE w.task_id=:task AND NOT EXISTS(SELECT 1 FROM text_experiment_work ew WHERE ew.work_id=w.work_id)
        ORDER BY t.created_at'''),{'task':task_id}).mappings().all()
    selected={(str(t['run_id']),t['step_id']):t for t in traces if t['phase']=='SELECT'}
    verified={(str(t['run_id']),t['step_id']):t for t in traces if t['phase']=='VERIFY'}
    for t in traces:
        if digest(t['snapshot'])!=t['snapshot_sha256']:raise Conflict('trace hash mismatch')
    labels=c.execute(text('''SELECT DISTINCT ON(l.run_id,l.step_id) l.* FROM memory_applicability_labels l
        JOIN runs r USING(run_id) JOIN work_items w USING(work_id) WHERE w.task_id=:task
        ORDER BY l.run_id,l.step_id,l.created_at DESC,l.label_id DESC'''),{'task':task_id}).mappings().all()
    recall_hit=recall_total=0
    for row in labels:
        if digest(row['snapshot'])!=row['snapshot_sha256']:raise Conflict('applicability label changed')
        expected=set(row['snapshot']['request']['expected_rule_ids'])
        trace=selected.get((str(row['run_id']),row['step_id']))
        if trace:
            actual=set(trace['snapshot']['selected_rule_ids']);recall_hit+=len(expected&actual);recall_total+=len(expected)
    conflict_count=pair_count=0
    for key,trace in selected.items():
        ids=trace['snapshot']['selected_rule_ids'];check=verified.get(key)
        terminal=trace['run_status'] not in ('RUNNING',)
        pinned={r['entry_id']:r for r in trace['manifest'].get('knowledge',{}).get('active',[]) if r['kind']=='RULE'}
        for rule_id in ids:
            stat=stats.setdefault(rule_id,{'selected':0,'verified':0,'complied':0,'failed':0,'pending':0});stat['selected']+=1
            if not check:
                stat['failed' if terminal else 'pending']+=1
                if terminal:stat['verified']+=1
                continue
            findings=[r for r in check['snapshot'].get('results',[]) if r.get('parent_rule_id')==rule_id]
            prerequisites=[r for r in check['snapshot'].get('preconditions',[]) if r['entry_id'].split('/')[0]==rule_id]
            if findings or prerequisites:
                passed=all(r['passed'] for r in findings+prerequisites)
                stat['verified']+=1;stat['complied' if passed else 'failed']+=1
            else:stat['pending']+=1
        for left,right in combinations(ids,2):
            pair_count+=1
            a=pinned.get(left,{}).get('payload',{});b=pinned.get(right,{}).get('payload',{})
            conflict=a.get('policy_slot')==b.get('policy_slot') and a.get('policy_value')!=b.get('policy_value')
            declared=one(c,'''SELECT 1 FROM knowledge_relations WHERE kind='CONFLICTS_WITH' AND created_at<=:at AND
                ((entry_id=CAST(:a AS uuid) AND target_entry_id=CAST(:b AS uuid)) OR
                 (entry_id=CAST(:b AS uuid) AND target_entry_id=CAST(:a AS uuid)))''',{'at':trace['created_at'],'a':left,'b':right})
            conflict_count+=int(conflict or bool(declared))
    comparisons=c.execute(text('''SELECT x.experiment_id,k.candidate_entry_id,r.result,r.result_sha256,r.verdict
        FROM text_experiments x JOIN text_experiment_knowledge k USING(experiment_id)
        JOIN text_experiment_results r USING(experiment_id) WHERE x.task_id=:task ORDER BY x.created_at'''),{'task':task_id}).mappings().all()
    regression_count=paired_count=0;utility={}
    for row in comparisons:
        if digest(row['result'])!=row['result_sha256']:raise Conflict('comparison result changed')
        result=row['result'];valid=[case for case in result['cases'] if case['champion']['valid_execution'] and case['challenger']['valid_execution']]
        paired_count+=len(valid);regression_count+=len(result['regressions'])
        utility.setdefault(str(row['candidate_entry_id']),[]).append({'experiment_id':str(row['experiment_id']),'verdict':row['verdict'],
            'quality_delta':result.get('quality_delta'),'suite_sha256':result['suite_sha256'],
            'scope':'FROZEN_PAIRED_REPLAY_NOT_PREVIOUS_RUN_SCORE_DIFFERENCE'})
    suggestions=[]
    by_id={str(r['entry_id']):r for r in rows}
    for entry_id,row in by_id.items():
        if row['kind']=='LESSON':suggestions.append({'entry_id':entry_id,'kind':'NORMALIZE_LEGACY','reason':'자유 교훈은 원본으로 보존하고 조건·행동·검사 후보로 전환하세요.'})
        if row['decision']=='APPLY' and stats[entry_id]['failed']>=2:
            suggestions.append({'entry_id':entry_id,'kind':'REVIEW_REPEATED_FAILURE','reason':'규칙 이행 검사 실패가 반복됐습니다. 원문을 확인해 정정·중지 여부를 검토하세요.'})
        if row['decision']=='APPLY' and len(selected)>=10 and stats[entry_id]['selected']==0:
            suggestions.append({'entry_id':entry_id,'kind':'REVIEW_UNUSED','reason':'관찰된10개 이상 단계에서 선택되지 않았습니다. 조건 적합성을 검토하세요. 무용하다는 판정은 아닙니다.'})
    for left,right in combinations([r for r in rows if r['kind']=='RULE' and r['decision']=='PROPOSED'],2):
        fields=('scope','target_steps','target_roles','when','unless','actions','policy_slot','policy_value')
        if all(left['payload'].get(k)==right['payload'].get(k) for k in fields):
            suggestions.append({'entry_id':str(right['entry_id']),'target_entry_id':str(left['entry_id']),'kind':'LINK_DUPLICATE',
                                'reason':'정규화된 조건·행동이 동일합니다. 원본은 유지하고 중복 관계와 근거를 연결하세요.'})
    compliance_n=sum(s['complied'] for s in stats.values());compliance_d=sum(s['verified'] for s in stats.values())
    return {'task_id':task_id,'metrics':{'applicable_recall':ratio(recall_hit,recall_total,'USER_LABELED_EXPECTED_RULES_ONLY'),
        'rule_compliance':ratio(compliance_n,compliance_d,'CONFIGURED_RULE_CHECKS_NOT_COMPLETE_PROSE_SEMANTICS'),
        'regression_rate':ratio(regression_count,paired_count,'VALID_SAME_SUITE_CASE_PAIRS_ONLY'),
        'conflict_rate':ratio(conflict_count,pair_count,'CO_SELECTED_DECLARED_OR_POLICY_SLOT_CONFLICTS_ONLY')},
        'rules':{key:{**stat,'knowledge_utility':utility.get(key)} for key,stat in stats.items()},
        'suggestions':suggestions,'policy':'SUGGEST_ONLY; USER_DECISION_REQUIRED; MISSING_DENOMINATORS_ARE_NULL'}
