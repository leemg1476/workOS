"""Record auditable, self-reported human effort against synthetic VOC inputs.

The label set is a demo fixture, not evidence of production accuracy or of a
real one-hour baseline. Work OS predictions must come from a verified Run.
"""
from typing import Literal
from datetime import timedelta
from zoneinfo import ZoneInfo
from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.domain.hashing import canonical, digest
from app.evaluation.poc_voc_fixtures import CATEGORIES, PERIODS, cases, records
from app.work.service import Kernel, Conflict, NotFound, one, uid
from app.work.voc import classify


class BaselineRequest(BaseModel):
    answers: dict[str,str] = Field(min_length=1,max_length=100)
    human_seconds: int = Field(ge=1,le=86400,strict=True)
    idempotency_key: str = Field(min_length=1,max_length=150)


class AssistedRequest(BaseModel):
    baseline_id: UUID
    run_id: UUID
    period: Literal['week1','week2']
    answers: dict[str,str] = Field(min_length=1,max_length=100)
    reviewed_ids: list[str] = Field(default_factory=list,max_length=100)
    human_seconds: int = Field(ge=1,le=86400,strict=True)
    idempotency_key: str = Field(min_length=1,max_length=150)


class ScheduleRequest(BaseModel):
    baseline_id: UUID
    idempotency_key: str = Field(min_length=1,max_length=150)


class ScheduleState(BaseModel):
    enabled: bool
    idempotency_key: str = Field(min_length=1,max_length=150)


def _inputs(c,period):
    expected=records(period)
    current=[dict(row) for row in c.execute(text('''SELECT record_id,source_version,body FROM voc_records
        WHERE source_version=:version ORDER BY record_id'''),{'version':PERIODS[period]}).mappings()]
    if current!=expected:
        raise Conflict('PoC input fixture is missing or changed')
    return current


def _answers(period,answers):
    ids={x['record_id'] for x in records(period)}
    if set(answers)!=ids or any(x not in CATEGORIES for x in answers.values()):
        raise Conflict('one valid answer is required for every fixture record')
    return {key:answers[key] for key in sorted(ids)}


def _score(period,answers):
    expected={row['record_id']:row['expected_category'] for row in cases(period)}
    return _score_against(expected,answers)


def _score_against(expected,answers):
    wrong=[key for key in sorted(expected) if answers[key]!=expected[key]]
    return {'correct':len(expected)-len(wrong),'total':len(expected),'incorrect_ids':wrong}


def _validate_measurement(row):
    snapshot=row['snapshot'];inputs=snapshot.get('inputs');labels=snapshot.get('gold_labels')
    request={'kind':row['kind'],'period':row['period'],'answers':row['answers'],'human_seconds':row['human_seconds']}
    if row['kind']=='ASSISTED':
        request.update(reviewed_ids=row['reviewed_ids'],baseline_id=str(row['baseline_id']),run_id=str(row['run_id']))
    if (not isinstance(inputs,list) or not isinstance(labels,dict) or row['time_basis']!='SELF_REPORTED'
        or snapshot.get('time_basis')!='SELF_REPORTED'
        or digest(snapshot)!=row['snapshot_sha256'] or snapshot.get('request_sha256')!=digest(request)
        or snapshot.get('fixture_sha256')!=digest(inputs)
        or set(labels)!={x['record_id'] for x in inputs} or set(row['answers'])!=set(labels)
        or snapshot.get('gold_sha256')!=digest([{**x,'expected_category':labels[x['record_id']]} for x in inputs])
        or snapshot.get('result')!=_score_against(labels,row['answers'])):
        raise Conflict('measurement evidence changed')
    if row['kind']=='ASSISTED':
        predictions=snapshot.get('run',{}).get('predictions',{})
        if (set(predictions)!=set(labels) or snapshot.get('raw_result')!=_score_against(labels,predictions)
            or snapshot.get('reviewed_count')!=len(row['reviewed_ids'])
            or snapshot.get('changed_ids')!=[key for key in sorted(row['answers']) if row['answers'][key]!=predictions[key]]):
            raise Conflict('measurement review evidence changed')


def _run(c,run_id,period):
    row=one(c,'''SELECT r.run_id,r.status AS run_status,r.model_id,r.agent_version,r.started_at,r.finished_at,w.work_id,w.status AS work_status,
        w.task_id,w.task_version,w.input_snapshot,w.accepted_artifact_id,a.artifact_id,a.content,a.sha256,
        ctx.manifest,ctx.sha256 AS context_sha,e.result AS validation,e.input_refs,e.verifier_version,ev.eval_suite_version
        FROM runs r JOIN work_items w ON w.work_id=r.work_id
        JOIN artifacts a ON a.run_id=r.run_id AND a.kind='voc_report'
        JOIN run_contexts ctx ON ctx.run_id=r.run_id
        JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='voc_deterministic'
        JOIN evaluations ev ON ev.artifact_id=a.artifact_id AND ev.verdict='PASS'
        WHERE r.run_id=:id''',{'id':run_id})
    if not row: raise NotFound('verified PoC run not found')
    manifest=row['manifest'];content=row['content'];dataset=_inputs(c,period)
    if (row['task_id']!='TASK-003' or row['run_status']!='SUCCEEDED' or row['work_status']!='COMMITTED'
        or row['accepted_artifact_id']!=row['artifact_id'] or row['model_id']!='MOCK'
        or row['input_snapshot'].get('source_version')!=PERIODS[period]
        or manifest.get('source_version')!=PERIODS[period] or manifest.get('records')!=dataset
        or manifest.get('records_sha256')!=digest(dataset) or manifest.get('run_id')!=str(row['run_id'])
        or manifest.get('work_id')!=str(row['work_id']) or manifest.get('task_version')!=row['task_version']
        or digest(manifest)!=row['context_sha'] or digest(content)!=row['sha256']
        or row['input_refs'].get('artifact_sha256')!=row['sha256']
        or row['input_refs'].get('context_sha256')!=row['context_sha']
        or row['verifier_version']!='voc-poc-validator-v1' or row['eval_suite_version']!='voc-poc-run-suite-v1'
        or row['validation'].get('passed') is not True or content.get('model_mode')!='MOCK'
        or content.get('data_mode')!='DEMO DATA' or content.get('source_version')!=PERIODS[period]
        or digest(manifest['method']['rules'])!=manifest['method']['sha256']
        or row['agent_version']!=manifest['method']['method_id'] or content.get('method_version')!=row['agent_version']):
        raise Conflict('PoC run or input evidence is not current and verified')
    predicted={}
    for bucket in content.get('recommendations',[]):
        category=bucket.get('category')
        if category not in CATEGORIES[:-1]: raise Conflict('invalid PoC prediction category')
        for record_id in bucket.get('record_ids',[]):
            if record_id in predicted: raise Conflict('duplicate PoC prediction')
            predicted[record_id]=category
    for record_id in content.get('unclassified_ids',[]):
        if record_id in predicted: raise Conflict('duplicate PoC prediction')
        predicted[record_id]='unclassified'
    if (set(predicted)!={r['record_id'] for r in dataset}
        or any(predicted[x['record_id']]!=classify(x['body'],manifest['method']['rules']) for x in dataset)):
        raise Conflict('PoC prediction does not match the pinned method and inputs')
    return {'run_id':str(row['run_id']),'work_id':str(row['work_id']),'artifact_id':str(row['artifact_id']),
            'started_at':row['started_at'].isoformat(),'finished_at':row['finished_at'].isoformat() if row['finished_at'] else None,
            'run_elapsed_ms':round((row['finished_at']-row['started_at']).total_seconds()*1000) if row['finished_at'] else None,
            'scheduled_for':row['input_snapshot'].get('scheduled_for'),
            'scheduled_baseline_id':row['input_snapshot'].get('poc_baseline_id'),
            'artifact_sha256':row['sha256'],'context_sha256':row['context_sha'],'task_version':row['task_version'],
            'method_version':row['agent_version'],'method_sha256':manifest['method']['sha256'],
            'verifier_version':row['verifier_version'],'suite_version':row['eval_suite_version'],
            'predictions':{k:predicted[k] for k in sorted(predicted)}}


def _prior(c,key,request):
    row=one(c,'SELECT measurement_id,snapshot FROM poc_voc_measurements WHERE idempotency_key=:key',{'key':key})
    if row:
        if row['snapshot'].get('request_sha256')!=digest(request): raise Conflict('measurement idempotency key already used')
        return str(row['measurement_id'])
    return None


def save_baseline(db,body):
    answers=_answers('week1',body.answers)
    request={'kind':'BASELINE','period':'week1','answers':answers,'human_seconds':body.human_seconds}
    return _save(db,body.idempotency_key,request)


def save_assisted(db,body):
    answers=_answers(body.period,body.answers);reviewed=sorted(set(body.reviewed_ids))
    if len(reviewed)!=len(body.reviewed_ids) or not set(reviewed)<=set(answers):
        raise Conflict('reviewed IDs must be unique fixture records')
    request={'kind':'ASSISTED','period':body.period,'answers':answers,'reviewed_ids':reviewed,
             'human_seconds':body.human_seconds,'baseline_id':str(body.baseline_id),'run_id':str(body.run_id)}
    return _save(db,body.idempotency_key,request)


def _save(db,key,request):
    kernel=Kernel(db);event_key='poc-voc.measurement:'+key
    with db.begin() as c:
        kernel._lock(c)
        prior=_prior(c,key,request)
        if prior: return prior,False
        period=request['period'];inputs=_inputs(c,period)
        snapshot={'request_sha256':digest(request),'fixture_sha256':digest(inputs),'gold_sha256':digest(cases(period)),
                  'inputs':inputs,'gold_labels':{x['record_id']:x['expected_category'] for x in cases(period)},
                  'result':_score(period,request['answers']),'time_basis':'SELF_REPORTED',
                  'data_mode':'SYNTHETIC_FIXTURE','semantic_scope':'FIXTURE_LABELS_ONLY'}
        if request['kind']=='ASSISTED':
            baseline=one(c,"SELECT * FROM poc_voc_measurements WHERE measurement_id=:id AND kind='BASELINE'",{'id':request['baseline_id']})
            if not baseline: raise NotFound('baseline measurement not found')
            _validate_measurement(baseline)
            if baseline['snapshot'].get('gold_sha256')!=digest(cases('week1')):
                raise Conflict('baseline evidence changed')
            run=_run(c,request['run_id'],period)
            if run['scheduled_baseline_id'] and run['scheduled_baseline_id']!=request['baseline_id']:
                raise Conflict('scheduled run belongs to a different baseline')
            if one(c,'SELECT started_at FROM runs WHERE run_id=:id',{'id':request['run_id']})['started_at']<baseline['created_at']:
                raise Conflict('the baseline must be recorded before the Work OS run')
            changed=[key for key in request['answers'] if request['answers'][key]!=run['predictions'][key]]
            if not set(changed)<=set(request['reviewed_ids']):
                raise Conflict('changed predictions must be marked reviewed')
            snapshot.update(run=run,raw_result=_score(period,run['predictions']),
                            changed_ids=changed,reviewed_count=len(request['reviewed_ids']),
                            baseline_snapshot_sha256=baseline['snapshot_sha256'])
        measurement=uid()
        c.execute(text('''INSERT INTO poc_voc_measurements(measurement_id,kind,period,baseline_id,run_id,human_seconds,time_basis,
            answers,reviewed_ids,snapshot,snapshot_sha256,idempotency_key)
            VALUES (:id,:kind,:period,:baseline,:run,:seconds,'SELF_REPORTED',CAST(:answers AS jsonb),CAST(:reviewed AS jsonb),
                CAST(:snapshot AS jsonb),:sha,:key)'''),
            {'id':measurement,'kind':request['kind'],'period':period,'baseline':request.get('baseline_id'),'run':request.get('run_id'),
             'seconds':request['human_seconds'],'answers':canonical(request['answers']),'reviewed':canonical(request.get('reviewed_ids',[])),
             'snapshot':canonical(snapshot),'sha':digest(snapshot),'key':key})
        kernel._event(c,'poc_voc.measurement_recorded',measurement,{'kind':request['kind'],'period':period,
            'snapshot_sha256':digest(snapshot),'time_basis':'SELF_REPORTED','data_mode':'SYNTHETIC_FIXTURE'},actor='user',key=event_key)
        return measurement,True


def list_measurements(db):
    with db.connect() as c:
        rows=c.execute(text('''SELECT measurement_id,kind,period,baseline_id,run_id,human_seconds,time_basis,
            answers,reviewed_ids,snapshot,snapshot_sha256,created_at FROM poc_voc_measurements ORDER BY created_at,measurement_id''')).mappings().all()
        result=[]
        for row in rows:
            _validate_measurement(row)
            result.append(dict(row))
        return result


def run_view(db,run_id,period):
    with db.connect() as c:
        return _run(c,run_id,period)


def create_schedule(db,body):
    """Opt in to a weekly Job; first due is at least seven days after baseline."""
    kernel=Kernel(db);key='poc-voc.schedule:'+body.idempotency_key
    request={'baseline_id':str(body.baseline_id)}
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request')!=request: raise Conflict('schedule idempotency key already used')
            return previous['subject_id'],False
        baseline=one(c,"SELECT * FROM poc_voc_measurements WHERE measurement_id=:id AND kind='BASELINE'",{'id':body.baseline_id})
        if not baseline: raise NotFound('baseline measurement not found')
        _validate_measurement(baseline)
        if one(c,'SELECT 1 FROM poc_voc_schedules WHERE baseline_id=:id',{'id':body.baseline_id}):
            raise Conflict('a schedule already exists for this baseline')
        task=one(c,"SELECT version FROM task_definitions WHERE task_id='TASK-003' AND active AND acceptance_policy->>'kind'='voc_report_v3'")
        if not task: raise Conflict('active VOC task is unavailable')
        first=baseline['created_at'].astimezone(ZoneInfo('Asia/Seoul'))+timedelta(days=7)
        if first.second or first.microsecond: first=(first+timedelta(minutes=1)).replace(second=0,microsecond=0)
        cron=f"{first.minute} {first.hour} * * {(first.weekday()+1)%7}"
        schedule_id=uid();job_id='TASK-003-POC-'+schedule_id
        preset={'source_version':PERIODS['week2'],'poc_baseline_id':str(body.baseline_id),'poc_schedule_id':schedule_id}
        c.execute(text('''INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type,schedule,timezone,enabled)
            VALUES (:job,'TASK-003',:version,'CRON',:schedule,'Asia/Seoul',true)'''),
            {'job':job_id,'version':task['version'],'schedule':cron})
        c.execute(text('''INSERT INTO job_trigger_presets(job_id,snapshot,snapshot_sha256,hidden_from_task_list)
            VALUES (:job,CAST(:snapshot AS jsonb),:sha,true)'''),{'job':job_id,'snapshot':canonical(preset),'sha':digest(preset)})
        from app.scheduler import next_due
        now=one(c,'SELECT clock_timestamp() AS current_time')['current_time']
        due=first if first>now else next_due(cron,'Asia/Seoul',now)
        c.execute(text('''INSERT INTO poc_voc_schedules(schedule_id,baseline_id,job_id,first_not_before)
            VALUES (:id,:baseline,:job,:first)'''),{'id':schedule_id,'baseline':body.baseline_id,'job':job_id,'first':first})
        c.execute(text('INSERT INTO job_schedule_cursors(job_id,next_due_at) VALUES (:job,:due)'),{'job':job_id,'due':due})
        kernel._event(c,'poc_voc.schedule_created',schedule_id,{'request':request,'job_id':job_id,'schedule':cron,
            'first_not_before':first.isoformat(),'preset_sha256':digest(preset),'data_mode':'SYNTHETIC_FIXTURE'},actor='user',key=key)
        return schedule_id,True


def set_schedule(db,schedule_id,body):
    kernel=Kernel(db);key='poc-voc.schedule-state:'+body.idempotency_key
    payload={'schedule_id':str(schedule_id),'enabled':body.enabled}
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload']!=payload: raise Conflict('schedule state idempotency key already used')
            return False
        row=one(c,'''SELECT s.job_id,s.first_not_before,j.enabled,j.schedule,j.timezone FROM poc_voc_schedules s JOIN job_definitions j ON j.job_id=s.job_id
            WHERE s.schedule_id=:id FOR UPDATE OF j''',{'id':schedule_id})
        if not row: raise NotFound('PoC schedule not found')
        if row['enabled']==body.enabled: raise Conflict('schedule already has the requested state')
        c.execute(text('UPDATE job_definitions SET enabled=:enabled WHERE job_id=:job'),{'enabled':body.enabled,'job':row['job_id']})
        c.execute(text('DELETE FROM job_schedule_cursors WHERE job_id=:job'),{'job':row['job_id']})
        if body.enabled:
            from app.scheduler import next_due
            now=one(c,'SELECT clock_timestamp() AS current_time')['current_time']
            due=row['first_not_before'] if row['first_not_before']>now else next_due(row['schedule'],row['timezone'],now)
            c.execute(text('INSERT INTO job_schedule_cursors(job_id,next_due_at) VALUES (:job,:due)'),{'job':row['job_id'],'due':due})
        kernel._event(c,'poc_voc.schedule_state_changed',schedule_id,payload,actor='user',key=key)
        return True


def list_schedules(db):
    with db.connect() as c:
        return [dict(row) for row in c.execute(text('''SELECT s.schedule_id,s.baseline_id,s.job_id,s.created_at,s.first_not_before,j.schedule,j.timezone,j.enabled,
            cursor.next_due_at,latest.work_id AS latest_work_id,latest.status AS latest_work_status,
            r.run_id AS latest_run_id,r.status AS latest_run_status
            FROM poc_voc_schedules s JOIN job_definitions j ON j.job_id=s.job_id
            LEFT JOIN job_schedule_cursors cursor ON cursor.job_id=s.job_id
            LEFT JOIN LATERAL (SELECT work_id,status FROM work_items WHERE job_id=s.job_id ORDER BY created_at DESC LIMIT 1) latest ON true
            LEFT JOIN LATERAL (SELECT run_id,status FROM runs WHERE work_id=latest.work_id ORDER BY attempt_no DESC LIMIT 1) r ON true
            ORDER BY s.created_at''')).mappings()]
