"""Explicit latest-new-since-last-delivery joins for human-reviewed text work."""
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.domain.hashing import canonical
from app.work.service import Kernel, Conflict, NotFound, one, uid
from app.workloads.text_links import _cycle, _text_task, validate_source


class CreateJoin(BaseModel):
    source_task_ids: list[str] = Field(min_length=2, max_length=8)
    target_task_id: str = Field(min_length=1,max_length=200)
    idempotency_key: str = Field(min_length=1,max_length=150)


class SetJoin(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=150)


def create(db,body):
    sources=sorted(body.source_task_ids)
    if len(set(sources))!=len(sources) or any(not x or len(x)>200 for x in sources):
        raise Conflict('sources must be distinct task IDs')
    kernel=Kernel(db);key='text-join.create:'+body.idempotency_key
    payload={'source_task_ids':sources,'target_task_id':body.target_task_id}
    with db.begin() as c:
        kernel._lock(c)
        prior=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if prior:
            if prior['payload']!=payload: raise Conflict('join idempotency key already used')
            return prior['subject_id'],False
        if not _text_task(c,body.target_task_id) or any(not _text_task(c,x) for x in sources):
            raise NotFound('active text task not found')
        if any(_cycle(c,x,body.target_task_id) for x in sources): raise Conflict('task link cycle')
        existing=one(c,'''SELECT j.join_id FROM text_work_joins j JOIN text_work_join_sources s ON s.join_id=j.join_id
            WHERE j.target_task_id=:target GROUP BY j.join_id
            HAVING array_agg(s.source_task_id ORDER BY s.source_task_id)=CAST(:sources AS text[])''',
            {'target':body.target_task_id,'sources':sources})
        if existing: raise Conflict('join already exists; enable it instead')
        join=uid();c.execute(text('INSERT INTO text_work_joins(join_id,target_task_id) VALUES (:id,:target)'),
                            {'id':join,'target':body.target_task_id})
        for source in sources:
            c.execute(text('INSERT INTO text_work_join_sources(join_id,source_task_id) VALUES (:id,:source)'),{'id':join,'source':source})
        kernel._event(c,'text_join.created',join,payload,actor='user',key=key)
        return join,True


def set_enabled(db,join_id,body):
    kernel=Kernel(db);key='text-join.set:'+body.idempotency_key
    payload={'join_id':str(join_id),'enabled':body.enabled,'expected_version':body.expected_version}
    with db.begin() as c:
        kernel._lock(c)
        prior=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if prior:
            if prior['payload']!=payload: raise Conflict('join state idempotency key already used')
            return False
        join=one(c,'SELECT * FROM text_work_joins WHERE join_id=:id FOR UPDATE',{'id':join_id})
        if not join: raise NotFound('join not found')
        if join['version']!=body.expected_version: raise Conflict('join changed')
        if join['enabled']==body.enabled: raise Conflict('join already has the requested state')
        sources=c.execute(text('SELECT source_task_id FROM text_work_join_sources WHERE join_id=:id'),{'id':join_id}).scalars().all()
        if body.enabled and (not _text_task(c,join['target_task_id']) or any(not _text_task(c,x) or _cycle(c,x,join['target_task_id']) for x in sources)):
            raise Conflict('join cannot be enabled')
        c.execute(text('UPDATE text_work_joins SET enabled=:enabled,version=version+1 WHERE join_id=:id'),
                  {'id':join_id,'enabled':body.enabled})
        kernel._event(c,'text_join.state_changed',join_id,payload,actor='user',prior=join['version'],key=key)
        return True


def pending(c,join):
    rows=c.execute(text('''SELECT s.source_task_id,a.arrival_id,a.source_work_id,a.source_artifact_id,a.source_artifact_sha256,
          w.status,w.task_id AS actual_task_id,w.accepted_artifact_id,art.sha256,art.content,r.work_id AS artifact_work_id
        FROM text_work_join_sources s
        LEFT JOIN LATERAL (SELECT x.* FROM text_work_join_arrivals x WHERE x.join_id=s.join_id AND x.join_version=:version
          AND x.source_task_id=s.source_task_id AND x.arrival_id>COALESCE((SELECT max(i.arrival_id)
            FROM text_work_join_firing_inputs i JOIN text_work_join_firings f ON f.firing_id=i.firing_id
            JOIN text_work_join_arrivals used ON used.arrival_id=i.arrival_id
            WHERE f.join_id=s.join_id AND f.join_version=:version AND used.source_task_id=s.source_task_id),0)
          ORDER BY x.arrival_id DESC LIMIT 1) a ON true
        LEFT JOIN work_items w ON w.work_id=a.source_work_id LEFT JOIN artifacts art ON art.artifact_id=a.source_artifact_id
        LEFT JOIN runs r ON r.run_id=art.run_id
        WHERE s.join_id=:join ORDER BY s.source_task_id'''),{'join':join['join_id'],'version':join['version']}).mappings().all()
    result=[]
    for item in rows:
        row=dict(item);state='WAITING'
        if row['arrival_id'] is not None:
            row['pinned_artifact_version']=row['source_artifact_id']
            try:
                if row['actual_task_id']!=row['source_task_id']: raise Conflict('joined source task changed')
                validate_source(c,row);state='READY'
            except Conflict: state='STALE'
        row['state']=state;result.append(row)
    return result


def collect(kernel,c,work,run_id,accepted):
    from app.workloads.text_sources import pin_source
    groups=c.execute(text('''SELECT j.* FROM text_work_joins j JOIN text_work_join_sources s ON s.join_id=j.join_id
        WHERE j.enabled AND s.source_task_id=:task ORDER BY j.join_id'''),{'task':work['task_id']}).mappings().all()
    for group in groups:
        already=one(c,'''SELECT 1 FROM text_work_join_arrivals WHERE join_id=:join AND join_version=:version AND source_artifact_id=:artifact''',
                    {'join':group['join_id'],'version':group['version'],'artifact':accepted['accepted_artifact_id']})
        if already: continue
        arrival=one(c,'''INSERT INTO text_work_join_arrivals(join_id,join_version,source_task_id,source_work_id,source_artifact_id,source_artifact_sha256)
            VALUES (:join,:version,:task,:work,:artifact,:sha) RETURNING arrival_id''',
            {'join':group['join_id'],'version':group['version'],'task':work['task_id'],'work':work['work_id'],
             'artifact':accepted['accepted_artifact_id'],'sha':accepted['sha256']})['arrival_id']
        kernel._event(c,'text_join.arrived',str(group['join_id']),{'join_version':group['version'],'arrival_id':arrival,
            'source_task_id':work['task_id'],'source_work_id':str(work['work_id']),'artifact_id':str(accepted['accepted_artifact_id']),
            'artifact_sha256':accepted['sha256']},cause=run_id)
        selected=pending(c,group)
        if len(selected)<2 or any(item['state']!='READY' for item in selected): continue
        target=_text_task(c,group['target_task_id'])
        if not target: raise Conflict('join target task is unavailable')
        target_id=uid();firing=uid()
        snapshot={'source':'accepted_work_join','join_id':str(group['join_id']),'join_version':group['version'],
                  'firing_id':firing,'input_count':len(selected)}
        c.execute(text('''INSERT INTO work_items(work_id,job_id,task_id,task_version,input_snapshot,status)
            VALUES (:id,:job,:task,:version,CAST(:snapshot AS jsonb),'CREATED')'''),
            {'id':target_id,'job':target['job_id'],'task':target['task_id'],'version':target['version'],'snapshot':canonical(snapshot)})
        pin_source(kernel,c,dict(target),target_id)
        kernel._event(c,'work.created',target_id,{'job_id':target['job_id'],'input_snapshot':snapshot},cause=run_id)
        c.execute(text('''INSERT INTO text_work_join_firings(firing_id,join_id,join_version,target_work_id)
            VALUES (:id,:join,:version,:target)'''),{'id':firing,'join':group['join_id'],'version':group['version'],'target':target_id})
        for item in selected:
            kernel._add_dependency(c,str(item['source_work_id']),target_id,'joined_text_artifact')
            c.execute(text('INSERT INTO text_work_join_firing_inputs(firing_id,arrival_id) VALUES (:firing,:arrival)'),
                      {'firing':firing,'arrival':item['arrival_id']})
        kernel._transition(c,target_id,'READY',run_id)
        c.execute(text("INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING')"),{'id':target_id})
        kernel._event(c,'text_join.fired',firing,{'join_id':str(group['join_id']),'join_version':group['version'],
            'target_work_id':target_id,'arrival_ids':[item['arrival_id'] for item in selected]},cause=run_id)


def joined_sources(c,work_id):
    firing=one(c,'SELECT * FROM text_work_join_firings WHERE target_work_id=:id',{'id':work_id})
    if not firing: return None
    rows=c.execute(text('''SELECT a.*,w.status,w.task_id AS actual_task_id,w.accepted_artifact_id,art.sha256,art.content,d.pinned_artifact_version,r.work_id AS artifact_work_id
        FROM text_work_join_firing_inputs i JOIN text_work_join_arrivals a ON a.arrival_id=i.arrival_id
        JOIN work_items w ON w.work_id=a.source_work_id JOIN artifacts art ON art.artifact_id=a.source_artifact_id JOIN runs r ON r.run_id=art.run_id
        JOIN work_dependencies d ON d.predecessor_id=a.source_work_id AND d.successor_id=:work
        WHERE i.firing_id=:firing ORDER BY a.source_task_id'''),{'firing':firing['firing_id'],'work':work_id}).mappings().all()
    expected=c.execute(text('SELECT source_task_id FROM text_work_join_sources WHERE join_id=:join ORDER BY source_task_id'),
                       {'join':firing['join_id']}).scalars().all()
    if (len(expected)<2 or [r['source_task_id'] for r in rows]!=expected
        or any(r['join_id']!=firing['join_id'] or r['join_version']!=firing['join_version'] or r['actual_task_id']!=r['source_task_id'] for r in rows)):
        raise Conflict('joined source set changed')
    result=[]
    for row in rows:
        validate_source(c,row)
        result.append({'task_id':row['source_task_id'],'work_id':str(row['source_work_id']),'artifact_id':str(row['source_artifact_id']),
                       'artifact_sha256':row['sha256'],'arrival_id':row['arrival_id'],'content':row['content']})
    return {'join_id':str(firing['join_id']),'join_version':firing['join_version'],'firing_id':str(firing['firing_id']),'sources':result}
