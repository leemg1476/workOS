"""Human-configured, internal links between text workloads; no external effects."""
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.domain.hashing import canonical, digest
from app.work.service import Kernel, Conflict, NotFound, one, uid


class CreateLink(BaseModel):
    source_task_id: str = Field(min_length=1, max_length=200)
    target_task_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=150)


class SetLink(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=150)


def _text_task(c, task):
    return one(c,'''SELECT t.task_id,t.version,j.job_id,j.enabled FROM task_definitions t
        JOIN job_definitions j ON j.task_id=t.task_id AND j.task_version=t.version
        WHERE t.task_id=:task AND t.active AND t.acceptance_policy->>'kind'='provided_text_workflow_v1' ''',{'task':task})


def _cycle(c, source, target, exclude=None):
    return source==target or bool(one(c,'''WITH RECURSIVE edges(source,target) AS (
        SELECT source_task_id,target_task_id FROM text_work_links WHERE enabled AND link_id IS DISTINCT FROM CAST(:exclude AS uuid)
        UNION SELECT s.source_task_id,j.target_task_id FROM text_work_join_sources s
          JOIN text_work_joins j ON j.join_id=s.join_id WHERE j.enabled), reachable(id) AS (
        SELECT target FROM edges WHERE source=:target
        UNION SELECT e.target FROM edges e JOIN reachable r ON e.source=r.id)
        SELECT 1 FROM reachable WHERE id=:source''',{'source':source,'target':target,'exclude':exclude}))


def create(db, body):
    kernel=Kernel(db);key='text-link.create:'+body.idempotency_key
    payload={'source_task_id':body.source_task_id,'target_task_id':body.target_task_id}
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload']!=payload: raise Conflict('link idempotency key already used')
            return previous['subject_id'],False
        if not _text_task(c,body.source_task_id) or not _text_task(c,body.target_task_id):
            raise NotFound('active text task not found')
        if _cycle(c,body.source_task_id,body.target_task_id): raise Conflict('task link cycle')
        if one(c,'SELECT 1 FROM text_work_links WHERE source_task_id=:source AND target_task_id=:target',
               {'source':body.source_task_id,'target':body.target_task_id}):
            raise Conflict('task link already exists; enable it instead')
        link=uid()
        c.execute(text('INSERT INTO text_work_links(link_id,source_task_id,target_task_id) VALUES (:id,:source,:target)'),
                  {'id':link,'source':body.source_task_id,'target':body.target_task_id})
        kernel._event(c,'text_link.created',link,payload,actor='user',key=key)
        return link,True


def set_enabled(db, link_id, body):
    kernel=Kernel(db);key='text-link.set:'+body.idempotency_key
    payload={'link_id':str(link_id),'enabled':body.enabled,'expected_version':body.expected_version}
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload']!=payload: raise Conflict('link state idempotency key already used')
            return False
        link=one(c,'SELECT * FROM text_work_links WHERE link_id=:id FOR UPDATE',{'id':link_id})
        if not link: raise NotFound('link not found')
        if link['version']!=body.expected_version: raise Conflict('link changed')
        if link['enabled']==body.enabled: raise Conflict('link already has the requested state')
        if body.enabled and (not _text_task(c,link['source_task_id']) or not _text_task(c,link['target_task_id'])
            or _cycle(c,link['source_task_id'],link['target_task_id'],str(link_id))):
            raise Conflict('link cannot be enabled')
        c.execute(text('UPDATE text_work_links SET enabled=:enabled,version=version+1 WHERE link_id=:id'),
                  {'id':link_id,'enabled':body.enabled})
        kernel._event(c,'text_link.state_changed',link_id,payload,actor='user',prior=link['version'],key=key)
        return True


def after_review(kernel,c,work,run_id):
    """Run under the same lock and transaction as human acceptance."""
    from app.workloads.text_sources import pin_source
    accepted=one(c,'''SELECT w.accepted_artifact_id,a.sha256,a.content FROM work_items w
        JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id WHERE w.work_id=:id AND w.status='COMMITTED' ''',{'id':work['work_id']})
    if not accepted or digest(accepted['content'])!=accepted['sha256']: raise Conflict('accepted source artifact changed')
    links=c.execute(text('SELECT * FROM text_work_links WHERE source_task_id=:task AND enabled ORDER BY link_id'),
                    {'task':work['task_id']}).mappings().all()
    for link in links:
        target=_text_task(c,link['target_task_id'])
        if not target: raise Conflict('linked target task is unavailable')
        if one(c,'SELECT 1 FROM text_work_link_deliveries WHERE link_id=:link AND source_artifact_id=:artifact',
               {'link':link['link_id'],'artifact':accepted['accepted_artifact_id']}):
            continue
        target_id=uid();delivery_id=uid()
        snapshot={'source':'accepted_work_link','link_id':str(link['link_id']),'source_work_id':str(work['work_id']),
                  'source_artifact_id':str(accepted['accepted_artifact_id'])}
        c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status)
            VALUES (:id,:job,:task,:version,CAST(:snapshot AS jsonb),'CREATED')'''),
            {'id':target_id,'job':target['job_id'],'task':target['task_id'],'version':target['version'],'snapshot':canonical(snapshot)})
        pin_source(kernel,c,dict(target),target_id)
        kernel._event(c,'work.created',target_id,{'job_id':target['job_id'],'input_snapshot':snapshot},cause=run_id)
        kernel._add_dependency(c,str(work['work_id']),target_id,'accepted_text_artifact')
        c.execute(text('''INSERT INTO text_work_link_deliveries(delivery_id,link_id,link_version,source_work_id,source_artifact_id,
            source_artifact_sha256,target_work_id) VALUES (:id,:link,:version,:source,:artifact,:sha,:target)'''),
            {'id':delivery_id,'link':link['link_id'],'version':link['version'],'source':work['work_id'],
             'artifact':accepted['accepted_artifact_id'],'sha':accepted['sha256'],'target':target_id})
        kernel._transition(c,target_id,'READY',run_id)
        c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"),{'id':target_id})
        kernel._event(c,'text_link.delivered',delivery_id,{'link_id':str(link['link_id']),'link_version':link['version'],
            'source_work_id':str(work['work_id']),'source_artifact_id':str(accepted['accepted_artifact_id']),
            'source_artifact_sha256':accepted['sha256'],'target_work_id':target_id},cause=run_id)
    from app.workloads.text_joins import collect
    collect(kernel,c,work,run_id,accepted)


def linked_source(c,work_id):
    row=one(c,'''SELECT l.source_work_id,l.source_artifact_id,l.source_artifact_sha256,l.link_id,l.link_version,
        w.status,w.accepted_artifact_id,a.content,a.sha256,d.pinned_artifact_version,r.work_id AS artifact_work_id
        FROM text_work_link_deliveries l JOIN work_items w ON w.work_id=l.source_work_id
        JOIN artifacts a ON a.artifact_id=l.source_artifact_id
        JOIN runs r ON r.run_id=a.run_id
        JOIN work_dependencies d ON d.predecessor_id=l.source_work_id AND d.successor_id=l.target_work_id
        WHERE l.target_work_id=:work''',{'work':work_id})
    if not row: return None
    validate_source(c,row)
    return {'link_id':str(row['link_id']),'link_version':row['link_version'],'work_id':str(row['source_work_id']),
            'artifact_id':str(row['source_artifact_id']),'artifact_sha256':row['sha256'],'content':row['content']}


def validate_source(c,row):
    if (row['status']!='COMMITTED' or row['accepted_artifact_id']!=row['source_artifact_id']
        or row['pinned_artifact_version']!=row['source_artifact_id'] or row['sha256']!=row['source_artifact_sha256']
        or row.get('artifact_work_id')!=row['source_work_id'] or digest(row['content'])!=row['sha256']):
        raise Conflict('linked source artifact is no longer current')
    if not one(c,'''SELECT 1 FROM evidence human JOIN evidence structural ON structural.artifact_id=human.artifact_id
        WHERE human.artifact_id=:artifact AND human.verification_type='human_work_review'
          AND human.result->>'decision'='ACCEPT' AND human.input_refs->>'artifact_sha256'=:sha
          AND structural.verification_type='provided_text_structure' AND structural.result->>'passed'='true'
          AND structural.input_refs->>'artifact_sha256'=:sha''',{'artifact':row['source_artifact_id'],'sha':row['sha256']}):
        raise Conflict('linked source verification or human review is missing')
