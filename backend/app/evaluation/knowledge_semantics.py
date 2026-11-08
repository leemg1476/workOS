"""Order-hidden user judgments; never synthesize a human evaluation from model scores."""
from typing import Literal
from uuid import UUID
from pydantic import Field
from sqlalchemy import text
from app.domain.experience import Contract
from app.domain.hashing import digest,canonical
from app.work.service import Kernel,Conflict,NotFound,one,uid


class CaseJudgment(Contract):
    case_id:UUID
    preference:Literal['A','B','TIE','NEITHER']
    critical_labels:list[Literal['A','B']]=Field(default_factory=list,max_length=2)
    note:str=Field(min_length=1,max_length=2000)


class SemanticReview(Contract):
    packet_sha256:str=Field(min_length=64,max_length=64)
    judgments:list[CaseJudgment]=Field(min_length=1,max_length=50)
    note:str=Field(min_length=1,max_length=2000)
    idempotency_key:str=Field(min_length=1,max_length=150)


def label_mapping(experiment_id,case_id):
    swapped=int(digest({'experiment':str(experiment_id),'case':case_id})[0],16)%2
    return {'A':'challenger' if swapped else 'champion','B':'champion' if swapped else 'challenger'}


def packet(c,experiment_id):
    exp=one(c,'''SELECT x.*,r.result,r.result_sha256 FROM text_experiments x JOIN text_experiment_results r USING(experiment_id)
        JOIN text_experiment_knowledge k USING(experiment_id) WHERE x.experiment_id=:id''',{'id':experiment_id})
    if not exp:raise NotFound('evaluated knowledge comparison not found')
    if digest(exp['result'])!=exp['result_sha256']:raise Conflict('evaluation result hash mismatch')
    suite=one(c,'SELECT snapshot,snapshot_sha256 FROM text_regression_suites WHERE suite_id=:id',{'id':exp['suite_id']})
    if digest(suite['snapshot'])!=suite['snapshot_sha256']:raise Conflict('suite changed')
    cases=[]
    for entry in suite['snapshot']['cases']:
        result=next(r for r in exp['result']['cases'] if r['case_id']==entry['case_id'])
        mapping=label_mapping(experiment_id,entry['case_id']);labels={}
        for label,side in mapping.items():
            actual=result[side]
            artifact=one(c,'SELECT content,sha256 FROM artifacts WHERE artifact_id=CAST(:id AS uuid)',{'id':actual.get('artifact_id')})
            if not artifact or digest(artifact['content'])!=artifact['sha256']:raise Conflict('review output missing or changed')
            step=result['champion_step_id' if side=='champion' else 'candidate_step_id']
            body=next(s['text'] for s in artifact['content']['steps'] if s['step_id']==step)
            labels[label]={'text':body,'sha256':digest(body)}
        cases.append({'case_id':entry['case_id'],'source_text':entry['case']['input_text'],
                      'expected_behavior':entry['case']['expected_behavior'],'outputs':labels})
    result={'experiment_id':str(experiment_id),'result_sha256':exp['result_sha256'],'cases':cases,
            'scope':'ORDER_HIDDEN_USER_JUDGMENT_NOT_EXPERT_GROUND_TRUTH',
            'instructions':'원천·가정·정확성과 일반 독자의 실용성을 비교하세요. 방법 이름과 자동 점수는 숨겼습니다. 오류가 있으면 해당 출력과 이유를 적으세요.'}
    return {**result,'packet_sha256':digest(result)}


def record(engine,experiment_id,body):
    kernel=Kernel(engine);key='knowledge.semantic-review:'+body.idempotency_key
    request={'experiment_id':str(experiment_id),**body.model_dump(mode='json')}
    with engine.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload']['request']!=request:raise Conflict('semantic review idempotency conflict')
            return previous['payload']['review_id']
        shown=packet(c,experiment_id)
        if shown['packet_sha256']!=body.packet_sha256:raise Conflict('review packet changed; read it again')
        required={r['case_id'] for r in shown['cases']}
        actual=[str(j.case_id) for j in body.judgments]
        if set(actual)!=required or len(actual)!=len(required):raise Conflict('review every case exactly once')
        verdicts=[]
        for j in body.judgments:
            mapping=label_mapping(experiment_id,str(j.case_id))
            preferred=mapping.get(j.preference,j.preference)
            critical=[mapping[label] for label in j.critical_labels]
            verdicts.append({'case_id':str(j.case_id),'preferred':preferred,'critical_sides':critical,
                             'no_candidate_regression':preferred in ('challenger','TIE') and 'challenger' not in critical})
        snapshot={'request':request,'case_results':verdicts,'scope':'EXPLICIT_USER_JUDGMENT_NOT_MODEL_OR_EXPERT_CERTIFICATION'}
        verdict='PASS' if all(v['no_candidate_regression'] for v in verdicts) else 'REJECT';review_id=uid()
        c.execute(text('''INSERT INTO knowledge_semantic_reviews(review_id,experiment_id,packet_sha256,snapshot,snapshot_sha256,verdict)
            VALUES (:id,:exp,:packet,CAST(:snapshot AS jsonb),:sha,:verdict)'''),
            {'id':review_id,'exp':experiment_id,'packet':body.packet_sha256,'snapshot':canonical(snapshot),'sha':digest(snapshot),'verdict':verdict})
        kernel._event(c,'knowledge.semantic_review',str(experiment_id),{'request':request,'review_id':review_id,'verdict':verdict},actor='user',key=key)
        return review_id


def require_human_review(c,experiment):
    review=one(c,'''SELECT * FROM knowledge_semantic_reviews WHERE experiment_id=:id
        ORDER BY created_at DESC,review_id DESC LIMIT 1''',{'id':experiment['experiment_id']})
    if not review:raise Conflict('human judgment required; AI scores cannot substitute for it')
    if digest(review['snapshot'])!=review['snapshot_sha256'] or review['verdict']!='PASS':raise Conflict('human judgment did not pass')
    shown=packet(c,experiment['experiment_id'])
    if shown['packet_sha256']!=review['packet_sha256']:raise Conflict('human judgment refers to different outputs')
    return review
