"""Automated reviewer opinions never approve Work or replace independent verification."""
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.work.service import Kernel, Conflict, NotFound, one, uid, canonical, digest


class AgentFeedback(BaseModel):
    artifact_id: str
    note: str = Field(min_length=1,max_length=2000)
    evaluator_model: str = Field(min_length=1,max_length=150)
    rubric_version: str = Field(min_length=1,max_length=150)
    metrics: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1,max_length=150)
    knowledge_to_retain: list[str] = Field(default_factory=list,max_length=8)


def record(engine, work_id, body):
    kernel=Kernel(engine)
    payload={'work_id':str(work_id),'artifact_id':body.artifact_id,'decision':'FEEDBACK',
             'note':body.note,'evaluator_model':body.evaluator_model,'rubric_version':body.rubric_version,
             'metrics':body.metrics,'scope':'AUTOMATED_OPINION_NOT_HUMAN_APPROVAL'}
    if body.knowledge_to_retain:payload['knowledge_to_retain']=body.knowledge_to_retain
    with engine.begin() as c:
        kernel._lock(c)
        key='work.agent_feedback:'+body.idempotency_key
        previous=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload']!=payload: raise Conflict('feedback idempotency key already used')
            return False
        work=kernel._work(c,work_id)
        artifact=one(c,'''SELECT a.*,r.status FROM artifacts a JOIN runs r ON r.run_id=a.run_id
            WHERE a.artifact_id=CAST(:id AS uuid) AND r.work_id=:work''',{'id':body.artifact_id,'work':work_id})
        if not artifact: raise NotFound('feedback artifact not found for work')
        if digest(artifact['content'])!=artifact['sha256']: raise Conflict('feedback artifact changed')
        c.execute(text('''INSERT INTO evidence(evidence_id,artifact_id,verification_type,input_refs,result,verifier_version)
            VALUES (:id,:artifact,'automated_report_review',CAST(:refs AS jsonb),CAST(:result AS jsonb),:version)'''),
            {'id':uid(),'artifact':body.artifact_id,'refs':canonical({'artifact_sha256':artifact['sha256']}),
             'result':canonical(payload),'version':body.rubric_version})
        kernel._event(c,'work.agent_feedback',work_id,payload,actor='evaluation-agent',cause=body.artifact_id,key=key)
        if not one(c,'SELECT 1 FROM text_experiment_work WHERE work_id=:id',{'id':work_id}):
            from app.work.experience import observe
            feedback_event=one(c,'SELECT event_id FROM work_events WHERE idempotency_key=:key',{'key':key})
            observe(kernel,c,work['task_id'],work['task_version'],body.artifact_id,feedback_event['event_id'],body.note)
            from app.work.knowledge import _insert,source_text
            context=one(c,'SELECT manifest FROM run_contexts WHERE run_id=:id',{'id':artifact['run_id']})
            source=context['manifest']['contract']['source_text'] if context and 'contract' in context['manifest'] else source_text(c,work['task_id'],work['task_version'])
            for lesson in body.knowledge_to_retain or [body.note]:
                if not lesson.strip() or len(lesson)>2000:raise Conflict('knowledge suggestion must be 1 to 2000 characters')
                _insert(kernel,c,work['task_id'],work['task_version'],digest(source),'LESSON','AI 제안 · 검토 필요',{'text':lesson},body.artifact_id)
    return True
