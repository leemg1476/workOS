"""Durable, version/input-scoped knowledge. AI proposes; users activate procedures."""
import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import text
from app.domain.hashing import canonical, digest
from app.work.service import Kernel, Conflict, NotFound, one, uid


class KnowledgeProposal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: Literal['LESSON','SOURCE_EXCERPT','CALCULATION','CHECKLIST','RULE']
    title: str=Field(min_length=1,max_length=150)
    payload: dict
    origin_artifact_id: UUID | None=None
    idempotency_key: str=Field(min_length=1,max_length=150)


class KnowledgeDecision(BaseModel):
    decision: Literal['APPLY','REJECT','RETIRE','ROLLBACK']
    expected_sha256: str=Field(min_length=64,max_length=64)
    note: str=Field(min_length=1,max_length=2000)
    idempotency_key: str=Field(min_length=1,max_length=150)
    evaluation_id: UUID | None=None
    replace_entries: dict[str,str] | None=None


def source_text(c, task_id, version):
    task=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:id AND version=:v',{'id':task_id,'v':version})
    if not task:return ''  # Empty history projections may query a not-yet-existing version.
    current=one(c,'SELECT source_text FROM text_source_versions WHERE task_id=:id ORDER BY version DESC LIMIT 1',{'id':task_id})
    return current['source_text'] if current else task['output_schema'].get('source_text','')


def calculate(expression, source, depth=0, budget=None):
    """Tiny Decimal AST, never eval/code/SQL. Numeric inputs only; no credentials or tools."""
    budget=budget if budget is not None else [100]
    budget[0]-=1
    if depth>8 or budget[0]<0 or not isinstance(expression,dict):raise ValueError('calculation too complex')
    if set(expression)=={'path'}:
        path=expression['path']
        if not isinstance(path,str) or not path.startswith('/'):raise ValueError('JSON pointer required')
        value=source
        for key in path[1:].split('/'):
            key=key.replace('~1','/').replace('~0','~')
            value=value[int(key)] if isinstance(value,list) else value[key]
    elif set(expression)=={'value'}:value=expression['value']
    elif set(expression)=={'op','args'}:
        op=expression['op'];args=expression['args']
        if not isinstance(args,list) or not 2<=len(args)<=20:raise ValueError('two to twenty operands required')
        values=[calculate(a,source,depth+1,budget) for a in args]
        with localcontext() as ctx:
            ctx.prec=28
            if op=='add':value=sum(values,Decimal(0))
            elif op=='multiply':
                value=Decimal(1)
                for operand in values:value*=operand
            elif op=='subtract' and len(values)==2:value=values[0]-values[1]
            elif op=='divide' and len(values)==2:value=values[0]/values[1]
            else:raise ValueError('unsupported arithmetic operation')
    else:raise ValueError('invalid expression')
    if isinstance(value,bool) or not isinstance(value,(int,float,str,Decimal)):raise ValueError('numeric input required')
    result=Decimal(str(value))
    if not result.is_finite() or abs(result)>Decimal('1e18'):raise ValueError('number out of range')
    return result


def validate_payload(kind,payload):
    if kind=='RULE':
        from app.domain.experience import ProceduralRule
        if len(canonical(payload))>12000:raise ValueError('rule payload exceeds budget')
        ProceduralRule.model_validate(payload)
        return payload
    if len(canonical(payload))>6000:raise ValueError('knowledge payload exceeds budget')
    fields={'LESSON':{'text'},'CHECKLIST':{'instruction','heading'},'SOURCE_EXCERPT':{'snapshot_id','quote'},
            'CALCULATION':{'expression','unit','decimal_places'}}[kind]
    if set(payload)!=fields:raise ValueError('unexpected or missing knowledge fields')
    for field in fields-{'expression','decimal_places'}:
        if not isinstance(payload[field],str) or not payload[field].strip():raise ValueError('nonempty text required')
    if kind=='CALCULATION':
        if type(payload['decimal_places']) is not int or not 0<=payload['decimal_places']<=8:raise ValueError('invalid decimal precision')
        if len(payload['unit'])>30:raise ValueError('unit too long')
    if kind=='SOURCE_EXCERPT':UUID(payload['snapshot_id'])
    return payload


def _insert(kernel,c,task_id,version,scope,kind,title,payload,artifact=None):
    validate_payload(kind,payload)
    value={'kind':kind,'title':title,'payload':payload,'source_scope_sha256':scope}
    sha=digest(value)
    existing=one(c,'SELECT entry_id FROM task_knowledge WHERE task_id=:task AND task_version=:v AND sha256=:sha',{'task':task_id,'v':version,'sha':sha})
    if existing:return str(existing['entry_id'])
    entry=uid()
    c.execute(text('''INSERT INTO task_knowledge(entry_id,task_id,task_version,kind,title,payload,source_scope_sha256,origin_artifact_id,sha256)
        VALUES (:id,:task,:v,:kind,:title,CAST(:payload AS jsonb),:scope,:artifact,:sha)'''),
        {'id':entry,'task':task_id,'v':version,'kind':kind,'title':title,'payload':canonical(payload),'scope':scope,'artifact':artifact,'sha':sha})
    kernel._event(c,'knowledge.proposed',task_id,{'entry_id':entry,'sha256':sha,'kind':kind},actor='knowledge-service')
    return entry


def propose(engine,task_id,body):
    kernel=Kernel(engine)
    request=body.model_dump(mode='json');key='knowledge.propose:'+body.idempotency_key
    with engine.begin() as c:
        kernel._lock(c)
        old=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if old:
            if old['payload']['request']!=request or old['payload']['task_id']!=task_id:raise Conflict('knowledge idempotency conflict')
            return old['payload']['entry_id']
        task=one(c,'SELECT version FROM task_definitions WHERE task_id=:id AND active',{'id':task_id})
        if not task:raise NotFound('active task not found')
        scope=digest(source_text(c,task_id,task['version']))
        if body.kind=='RULE':
            from app.work.procedural_knowledge import validate_rule
            validate_rule(c,task_id,task['version'],body.payload,source_text(c,task_id,task['version']))
        if body.origin_artifact_id:
            if not one(c,'''SELECT 1 FROM artifacts a JOIN runs r USING(run_id) JOIN work_items w USING(work_id)
                WHERE a.artifact_id=:id AND w.task_id=:task AND w.task_version=:v''',
                {'id':body.origin_artifact_id,'task':task_id,'v':task['version']}):raise Conflict('origin belongs to another task/version')
        entry=_insert(kernel,c,task_id,task['version'],scope,body.kind,body.title,body.payload,body.origin_artifact_id)
        kernel._event(c,'knowledge.proposal_requested',task_id,{'request':request,'task_id':task_id,'entry_id':entry},actor='user',key=key)
        return entry


def entries(c,task_id):
    return [dict(r) for r in c.execute(text('''SELECT k.*,a.run_id AS origin_run_id,r.work_id AS origin_work_id,
        COALESCE(d.decision,'PROPOSED') AS decision FROM task_knowledge k
        LEFT JOIN artifacts a ON a.artifact_id=k.origin_artifact_id LEFT JOIN runs r ON r.run_id=a.run_id
        LEFT JOIN LATERAL (SELECT decision FROM task_knowledge_decisions WHERE entry_id=k.entry_id
          ORDER BY created_at DESC,decision_id DESC LIMIT 1) d ON true
        WHERE k.task_id=:task ORDER BY k.created_at,k.entry_id'''),{'task':task_id}).mappings()]


def _resolve(c,entry,source):
    payload=entry['payload'];kind=entry['kind']
    if digest({'kind':kind,'title':entry['title'],'payload':payload,'source_scope_sha256':entry['source_scope_sha256']})!=entry['sha256']:
        raise ValueError('knowledge hash changed')
    result={k:str(entry[k]) for k in ('entry_id','sha256','kind','title')}
    if kind=='RULE':
        from app.domain.experience import ProceduralRule
        result.update(payload=ProceduralRule.model_validate(payload).model_dump(),scope='NORMALIZED_PROCEDURE_NOT_FACT_MEMORY')
    elif kind=='CALCULATION':
        number=calculate(payload['expression'],json.loads(source))
        value=number.quantize(Decimal(1).scaleb(-payload['decimal_places']))
        result.update(value=str(value),unit=payload['unit'],scope='DETERMINISTIC_DECIMAL_RESULT',expression=payload['expression'])
    elif kind=='SOURCE_EXCERPT':
        original=one(c,'SELECT * FROM report_source_snapshots WHERE snapshot_id=CAST(:id AS uuid)',{'id':payload['snapshot_id']})
        if not original:raise ValueError('source snapshot missing')
        owner=one(c,'''SELECT w.task_id,w.task_version,ctx.manifest->>'source_sha256' AS input_scope FROM runs r
            JOIN work_items w USING(work_id) JOIN run_contexts ctx ON ctx.run_id=r.run_id WHERE r.run_id=:id''',{'id':original['run_id']})
        if owner['task_id']!=entry['task_id'] or owner['task_version']!=entry['task_version'] or owner['input_scope']!=entry['source_scope_sha256']:
            raise ValueError('source snapshot belongs to another task/version/input')
        latest=one(c,'''SELECT s.* FROM report_source_snapshots s JOIN runs r USING(run_id) JOIN work_items w USING(work_id)
            JOIN run_contexts ctx ON ctx.run_id=r.run_id WHERE w.task_id=:task AND w.task_version=:v AND s.url=:url
            AND ctx.manifest->>'source_sha256'=:scope ORDER BY s.captured_at DESC,s.snapshot_id DESC LIMIT 1''',
            {'task':entry['task_id'],'v':entry['task_version'],'url':original['url'],'scope':entry['source_scope_sha256']})
        if digest(latest['text_content'])!=latest['text_sha256'] or payload['quote'] not in latest['text_content']:raise ValueError('source quote changed or not supported')
        result.update(quote=payload['quote'],snapshot_id=str(latest['snapshot_id']),url=latest['url'],text_sha256=latest['text_sha256'],
                      scope='EXACT_SOURCE_EXCERPT_AS_CAPTURED_NOT_INTERPRETATION_VERIFIED')
    else:result.update(payload=payload,scope='USER_APPROVED_PROCEDURE_NOT_VERIFIED_FACT')
    return result


def decide(engine,entry_id,body):
    kernel=Kernel(engine);key='knowledge.decision:'+body.idempotency_key
    request={'entry_id':str(entry_id),**body.model_dump(mode='json',exclude_none=True)}
    with engine.begin() as c:
        kernel._lock(c)
        old=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if old:
            if old['payload']!=request:raise Conflict('decision idempotency conflict')
            return False
        entry=one(c,'SELECT * FROM task_knowledge WHERE entry_id=:id',{'id':entry_id})
        if not entry:raise NotFound('knowledge entry not found')
        if entry['sha256']!=body.expected_sha256 or not body.note.strip():raise Conflict('knowledge changed or decision reason missing')
        previous=one(c,'SELECT decision FROM task_knowledge_decisions WHERE entry_id=:id ORDER BY created_at DESC,decision_id DESC LIMIT 1',{'id':entry_id})
        state=previous['decision'] if previous else 'PROPOSED'
        allowed={'PROPOSED':{'APPLY','REJECT'},'APPLY':{'RETIRE'},'RETIRE':{'APPLY','ROLLBACK'},'REJECT':set()}
        if body.decision not in allowed[state]:raise Conflict('invalid knowledge decision')
        source=source_text(c,entry['task_id'],entry['task_version'])
        if body.decision in ('APPLY','ROLLBACK'):
            if body.decision=='ROLLBACK' and (entry['kind']!='RULE' or not body.replace_entries or not
                one(c,"SELECT 1 FROM task_knowledge_decisions WHERE entry_id=:id AND decision='APPLY'",{'id':entry_id})):
                raise Conflict('rollback requires a previously applied rule and an explicit current replacement')
            current=one(c,'SELECT version FROM task_definitions WHERE task_id=:id AND active',{'id':entry['task_id']})
            if entry['kind']=='RULE':
                from app.work.procedural_knowledge import applicable
                if not current or not applicable(c,entry,entry['task_id'],current['version'],source)[0]:raise Conflict('procedural rule contract changed')
            else:
                if not current or current['version']!=entry['task_version']:raise Conflict('task version changed')
                if digest(source)!=entry['source_scope_sha256']:raise Conflict('task input changed; propose scoped knowledge again')
            try:_resolve(c,entry,source)
            except (ValueError,KeyError,IndexError,TypeError,ArithmeticError) as exc:raise Conflict('knowledge proof or calculation is invalid') from exc
            from app.work.procedural_knowledge import candidates,applicable,activation_conflicts
            active=[r for r in candidates(c,entry['task_id']) if r['decision']=='APPLY' and
                (applicable(c,r,entry['task_id'],current['version'],source)[0] if r['kind']=='RULE' else
                 r['task_version']==current['version'] and r['source_scope_sha256']==digest(source))]
            replacements=body.replace_entries or {}
            for old_id,sha in replacements.items():
                old=next((r for r in active if str(r['entry_id'])==old_id and r['task_id']==entry['task_id']),None)
                if not old or old['sha256']!=sha or old_id==str(entry_id):raise Conflict('replacement is no longer the expected active entry')
                link=one(c,"SELECT 1 FROM knowledge_relations WHERE ((entry_id=:id AND target_entry_id=CAST(:old AS uuid)) OR (target_entry_id=:id AND entry_id=CAST(:old AS uuid))) AND kind='SUPERSEDES'",{'id':entry_id,'old':old_id})
                if not link:raise Conflict('explicit supersedes relation required for replacement')
                if body.decision=='ROLLBACK' and not one(c,"SELECT 1 FROM knowledge_relations WHERE entry_id=CAST(:old AS uuid) AND target_entry_id=:id AND kind='SUPERSEDES'",{'old':old_id,'id':entry_id}):raise Conflict('rollback must restore the superseded predecessor')
            activation_conflicts(c,entry,active,replacements)
            active=[r for r in active if str(r['entry_id']) not in replacements]
            if len(active)>=20:raise Conflict('active knowledge budget reached; retire obsolete entries first')
            if sum(len(canonical(r['payload'])) for r in active)+len(canonical(entry['payload']))>24000:raise Conflict('active knowledge text budget reached')
            if entry['kind']=='RULE':
                from app.evaluation.knowledge_experiments import require_evaluation
                require_evaluation(c,entry,body.evaluation_id,replacements,rollback=body.decision=='ROLLBACK')
            for old_id in replacements:
                c.execute(text("INSERT INTO task_knowledge_decisions(decision_id,entry_id,decision,note,actor) VALUES (:id,CAST(:old AS uuid),'RETIRE',:note,'user')"),{'id':uid(),'old':old_id,'note':'Explicit replacement: '+body.note})
                kernel._event(c,'knowledge.replaced',old_id,{'replacement_entry_id':str(entry_id),'note':body.note},actor='user')
        c.execute(text('INSERT INTO task_knowledge_decisions(decision_id,entry_id,decision,note,actor) VALUES (:id,:entry,:decision,:note,\'user\')'),
            {'id':uid(),'entry':entry_id,'decision':'APPLY' if body.decision=='ROLLBACK' else body.decision,'note':('ROLLBACK; latest suite not revalidated: ' if body.decision=='ROLLBACK' else '')+body.note})
        kernel._event(c,'knowledge.decision',entry['task_id'],request,actor='user',key=key)
        return True


def active_snapshot(c,task_id,version,source):
    active=[];excluded=[]
    from app.work.procedural_knowledge import candidates,applicable
    for entry in candidates(c,task_id):
        if entry['decision']!='APPLY':continue
        if entry['kind']=='RULE':
            matches,reason=applicable(c,entry,task_id,version,source)
            if not matches:
                excluded.append({'entry_id':str(entry['entry_id']),'reason':reason});continue
        elif entry['task_version']!=version or entry['source_scope_sha256']!=digest(source):
            excluded.append({'entry_id':str(entry['entry_id']),'reason':'TASK_VERSION_OR_INPUT_CHANGED'});continue
        try:active.append(_resolve(c,entry,source))
        except (ValueError,KeyError,IndexError,TypeError,ArithmeticError):
            excluded.append({'entry_id':str(entry['entry_id']),'reason':'SOURCE_OR_CALCULATION_PROOF_INVALID'})
    return {'version':'task-knowledge-v1','active':active,'excluded':excluded,'sha256':digest(active)}


def import_feedback(engine,task_id):
    """Idempotent historical import to proposals only; never activate old AI opinions."""
    kernel=Kernel(engine);ids=[]
    with engine.begin() as c:
        kernel._lock(c)
        if not one(c,'SELECT 1 FROM task_definitions WHERE task_id=:id',{'id':task_id}):raise NotFound('task not found')
        rows=c.execute(text('''SELECT ev.payload,w.task_version,ctx.manifest FROM work_events ev
            JOIN work_items w ON w.work_id::text=ev.subject_id
            JOIN artifacts a ON a.artifact_id::text=ev.payload->>'artifact_id'
            JOIN run_contexts ctx ON ctx.run_id=a.run_id
            WHERE ev.event_type='work.agent_feedback' AND w.task_id=:task
              AND NOT EXISTS (SELECT 1 FROM text_experiment_work x WHERE x.work_id=w.work_id)
            ORDER BY ev.created_at,ev.event_id'''),{'task':task_id}).mappings().all()
        for row in rows:
            source=row['manifest'].get('contract',{}).get('source_text')
            if source is None:continue
            for lesson in row['payload'].get('knowledge_to_retain') or [row['payload']['note']]:
                ids.append(_insert(kernel,c,task_id,row['task_version'],digest(source),'LESSON','AI 제안 · 검토 필요',{'text':lesson},row['payload']['artifact_id']))
    return ids


def reconcile_sources(engine,refs):
    """Target only accepted Work whose pinned source quote is contradicted by readback."""
    kernel=Kernel(engine)
    for ref in refs:
        if ref.get('status')!='CAPTURED':continue
        with engine.begin() as c:
            kernel._lock(c)
            source=one(c,'''SELECT s.*,w.task_id,w.task_version,ctx.manifest->>'source_sha256' AS input_scope
                FROM report_source_snapshots s JOIN runs r USING(run_id) JOIN work_items w USING(work_id)
                JOIN run_contexts ctx ON ctx.run_id=r.run_id WHERE s.snapshot_id=CAST(:id AS uuid)''',{'id':ref['snapshot_id']})
            rows=c.execute(text('''SELECT w.work_id,m.manifest FROM work_items w
                JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id JOIN runs r ON r.run_id=a.run_id
                JOIN run_task_memories m ON m.run_id=r.run_id JOIN run_contexts ctx ON ctx.run_id=r.run_id
                WHERE w.status='COMMITTED' AND w.task_id=:task AND w.task_version=:v AND ctx.manifest->>'source_sha256'=:scope'''),
                {'task':source['task_id'],'v':source['task_version'],'scope':source['input_scope']}).mappings().all()
            for row in rows:
                items=row['manifest'].get('knowledge',{}).get('active',[])
                if any(e['kind']=='SOURCE_EXCERPT' and e['url']==source['url'] and e['quote'] not in source['text_content'] for e in items):
                    kernel._transition(c,row['work_id'],'STALE',str(source['snapshot_id']))
                    kernel.stale_descendants(c,row['work_id'],str(source['snapshot_id']))
                    kernel._event(c,'knowledge.source_changed',row['work_id'],{'snapshot_id':str(source['snapshot_id']),'url':source['url']})


def guard_review_sources(c,work_id):
    context=one(c,'''SELECT ctx.manifest FROM runs r JOIN run_contexts ctx USING(run_id)
        WHERE r.work_id=:id ORDER BY r.attempt_no DESC LIMIT 1''',{'id':work_id})
    if not context:return
    memory=context['manifest'].get('task_memory',{}).get('manifest',{})
    for item in memory.get('knowledge',{}).get('active',[]):
        if item['kind']!='SOURCE_EXCERPT':continue
        entry=one(c,'SELECT * FROM task_knowledge WHERE entry_id=CAST(:id AS uuid)',{'id':item['entry_id']})
        try:_resolve(c,entry,context['manifest']['contract']['source_text'])
        except (ValueError,TypeError,KeyError) as exc:raise Conflict('knowledge source changed; regenerate the report before accepting') from exc
