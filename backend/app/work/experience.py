"""Immutable experience observations and explicit normalization candidates."""
from pydantic import Field
from app.domain.experience import Contract,ProceduralRule
from app.domain.hashing import digest,canonical
from app.work.service import Kernel,Conflict,NotFound,one,uid
from sqlalchemy import text


class NormalizeRequest(Contract):
    rule: ProceduralRule
    title: str=Field(min_length=1,max_length=150)
    note: str=Field(min_length=1,max_length=2000)
    idempotency_key: str=Field(min_length=1,max_length=150)


def classify_hint(note):
    """Routing hint only. A keyword match never proves a fault or its root cause."""
    categories=[('TECHNICAL_FAILURE',('연결 실패','timeout','api 연결')),
                ('OUTPUT_OMISSION',('누락','빠졌','산식 설명')),
                ('REASONING_ERROR',('인과','추론 오류','일반화')),
                ('FACT_ERROR',('수치 오류','부호 오류','통계 오류')),
                ('PROCEDURE_ERROR',('선행','조회하지','절차'))]
    folded=note.casefold()
    return next((kind for kind,terms in categories if any(t in folded for t in terms)),'UNCLASSIFIED')


def observe(kernel,c,task_id,version,artifact_id,event_id,note):
    classification=classify_hint(note)
    snapshot={'note':note,'classification':classification,'classification_scope':'UNVERIFIED_ROUTING_HINT',
              'root_cause_status':'REQUIRES_REVIEW','artifact_id':str(artifact_id),'event_id':str(event_id)}
    observation=uid()
    c.execute(text('''INSERT INTO experience_observations(observation_id,task_id,task_version,artifact_id,event_id,
        classification,snapshot,snapshot_sha256) VALUES (:id,:task,:v,:a,:event,:kind,CAST(:snapshot AS jsonb),:sha)'''),
        {'id':observation,'task':task_id,'v':version,'a':artifact_id,'event':event_id,'kind':classification,'snapshot':canonical(snapshot),'sha':digest(snapshot)})
    kernel._event(c,'experience.observed',task_id,{'observation_id':observation,'snapshot_sha256':digest(snapshot)},actor='experience-service')
    return observation


def normalize(engine,entry_id,request):
    from app.work.knowledge import _insert,source_text
    from app.work.procedural_knowledge import validate_rule
    kernel=Kernel(engine);key='knowledge.normalize:'+request.idempotency_key
    body={'entry_id':entry_id,**request.model_dump(mode='json')}
    with engine.begin() as c:
        kernel._lock(c)
        old=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if old:
            if old['payload']['request']!=body:raise Conflict('normalization idempotency conflict')
            return old['payload']['candidate_entry_id']
        original=one(c,'SELECT * FROM task_knowledge WHERE entry_id=:id',{'id':entry_id})
        if not original:raise NotFound('source knowledge not found')
        task=one(c,'SELECT version FROM task_definitions WHERE task_id=:id AND active',{'id':original['task_id']})
        source=source_text(c,original['task_id'],task['version']);payload=request.rule.model_dump()
        validate_rule(c,original['task_id'],task['version'],payload,source)
        candidate=_insert(kernel,c,original['task_id'],task['version'],digest(source),'RULE',request.title,payload,original['origin_artifact_id'])
        if candidate==entry_id:raise Conflict('normalization must produce a new version')
        c.execute(text('''INSERT INTO knowledge_relations(relation_id,entry_id,target_entry_id,kind,note)
            VALUES (:id,:new,:old,'DERIVED_FROM',:note) ON CONFLICT(entry_id,target_entry_id,kind) DO NOTHING'''),
            {'id':uid(),'new':candidate,'old':entry_id,'note':request.note})
        kernel._event(c,'knowledge.normalized',original['task_id'],{'request':body,'candidate_entry_id':candidate,
            'original_sha256':original['sha256'],'status':'PROPOSED_NOT_APPLIED'},actor='user',key=key)
        return candidate
