"""Human-authored, redacted regression cases and immutable suite snapshots."""
import re
import os,json
from typing import Annotated
from uuid import UUID
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from app.work.market import digest
from app.work.service import Kernel, Conflict, NotFound, canonical, one, uid
from app.evaluation.rule_regression import DomainAssertion
from typing import Literal

Phrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PII_PATTERNS = (
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    re.compile(r'(?<!\d)(?:01[016789]|02|0[3-6][1-5])[- .]?\d{3,4}[- .]?\d{4}(?!\d)'),
    re.compile(r'(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)'),
)


class TextCaseRequest(BaseModel):
    artifact_id: UUID
    step_id: str = Field(min_length=1, max_length=100)
    input_text: str = Field(min_length=1, max_length=8000)
    expected_behavior: str = Field(min_length=1, max_length=2000)
    required_phrases: list[Phrase] = Field(default_factory=list, max_length=20)
    forbidden_phrases: list[Phrase] = Field(default_factory=list, max_length=20)
    feedback: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=150)
    domain_assertions: list[DomainAssertion] = Field(default_factory=list,max_length=20)
    split: Literal['REGRESSION','HELDOUT'] = 'REGRESSION'
    synthetic_fixture: bool = False


class SuiteRequest(BaseModel):
    case_ids: list[UUID] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=150)


def _clean_case(request):
    values=[request.step_id,request.input_text,request.expected_behavior,request.feedback,*request.required_phrases,*request.forbidden_phrases]
    if any(not v.strip() for v in values):
        raise ValueError('입력, 기대 결과, 피드백은 비워둘 수 없습니다')
    if not request.required_phrases and not request.forbidden_phrases and not request.domain_assertions:
        raise ValueError('검사할 필수 문구 또는 금지 문구를 하나 이상 입력하세요')
    if any(pattern.search(v) for v in values for pattern in PII_PATTERNS):
        raise ValueError('이메일, 전화번호, 주민등록번호로 보이는 내용을 제거한 뒤 등록하세요')
    required=[x.strip() for x in request.required_phrases]
    forbidden=[x.strip() for x in request.forbidden_phrases]
    if len({x.casefold() for x in required})!=len(required) or len({x.casefold() for x in forbidden})!=len(forbidden):
        raise ValueError('중복 문구는 사용할 수 없습니다')
    if any(f.casefold() in r.casefold() for r in required for f in forbidden):
        raise ValueError('필수 문구에 금지 문구가 포함되어 함께 만족할 수 없습니다')
    case={'step_id':request.step_id.strip(),'input_text':request.input_text.strip(),
            'expected_behavior':request.expected_behavior.strip(),'feedback':request.feedback.strip(),
            'required_phrases':required,'forbidden_phrases':forbidden,
            'evaluation_scope':'CASE_INSENSITIVE_SUBSTRING_ONLY_NOT_SEMANTIC',
            'privacy_scope':'USER_REDACTED_WITH_LIMITED_PATTERN_SCREENING'}
    if request.domain_assertions:
        case.update(domain_assertions=[a.model_dump() for a in request.domain_assertions],split=request.split,
                    evaluation_scope='SUBSTRING_AND_STRUCTURED_DOMAIN_ASSERTIONS_NOT_FULL_PROSE_SEMANTICS')
        # Validate the fixed oracle against the input when the suite is authored,
        # not against whichever answer a challenger happens to produce.
        from app.work.procedural_knowledge import source_object
        from app.domain.experience import pointer
        from app.work.knowledge import calculate
        source=source_object(request.input_text)
        for assertion in request.domain_assertions:
            if assertion.formula:calculate(assertion.formula,source)
            for path in assertion.input_paths+assertion.source_paths:pointer(source,path)
    elif request.split!='REGRESSION':case['split']=request.split
    return case


def add_case(db, task_id, request):
    case = _clean_case(request)
    if request.synthetic_fixture:
        if os.getenv('WORKOS_ALLOW_SYNTHETIC_REVIEW_FIXTURES')!='1':raise Conflict('synthetic fixture authoring is not enabled')
        case['authoring_scope']='SYNTHETIC_FIXTURE_ORACLE_NOT_HUMAN_QUALITY_LABEL'
    request_hash=digest({'task_id':task_id,'artifact_id':str(request.artifact_id),'case':case})
    key='text-case:'+request.idempotency_key
    kernel=Kernel(db)
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request_sha256')!=request_hash:
                raise Conflict('case idempotency key already used')
            return previous['subject_id'],False
        source=one(c,'''SELECT a.artifact_id,a.content,a.sha256,r.run_id,r.status AS run_status,w.work_id,w.task_id,w.task_version,
            ctx.manifest,ctx.sha256 AS context_sha256,e.evidence_id,e.result AS review,e.input_refs AS review_refs,e.verifier_version AS review_verifier
            FROM artifacts a JOIN runs r ON r.run_id=a.run_id JOIN work_items w ON w.work_id=r.work_id
            JOIN run_contexts ctx ON ctx.run_id=r.run_id
            JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type=:review_type
            WHERE a.artifact_id=:artifact AND a.kind='provided_text_report' AND w.task_id=:task
              AND EXISTS (SELECT 1 FROM evidence v WHERE v.artifact_id=a.artifact_id
                  AND v.verification_type='provided_text_structure' AND v.result->>'passed'='true'
                  AND v.result->>'requires_human_review'='true')
            ORDER BY e.created_at DESC LIMIT 1''',{'artifact':request.artifact_id,'task':task_id,
                'review_type':'provided_text_structure' if request.synthetic_fixture else 'human_work_review'})
        if not source:
            raise Conflict('a human-reviewed result from this task is required')
        if (not isinstance(source['content'],dict) or not isinstance(source['manifest'],dict)
            or not isinstance(source['review_refs'],dict) or not isinstance(source['review'],dict)
            or source['run_status']!='SUCCEEDED' or (source['review'].get('passed') is not True if request.synthetic_fixture else source['review'].get('decision') not in ('ACCEPT','REJECT'))
            or digest(source['content'])!=source['sha256'] or digest(source['manifest'])!=source['context_sha256']
            or source['review_refs'].get('artifact_sha256')!=source['sha256']
            or source['manifest'].get('run_id')!=str(source['run_id'])
            or source['manifest'].get('task_id')!=task_id
            or source['manifest'].get('task_version')!=source['task_version']
            or digest(source['manifest'].get('contract'))!=source['content'].get('contract_sha256')):
            raise Conflict('reviewed result or input provenance failed verification')
        if request.synthetic_fixture:
            try:synthetic=json.loads(source['manifest'].get('contract',{}).get('source_text','')).get('data_kind')=='SYNTHETIC_TEST'
            except (ValueError,TypeError,AttributeError):synthetic=False
            if not synthetic:raise Conflict('fixture cases require an explicitly synthetic source episode')
        if case['step_id'] not in [s.get('step_id') for s in source['content'].get('steps',[])]:
            raise ValueError('평가 대상 단계가 결과에 없습니다')
        # The user supplies redacted test text explicitly; it is not asserted to be the original source.
        case.update(task_id=task_id,base_task_version=source['task_version'],source_artifact_id=str(source['artifact_id']),
                    source_artifact_sha256=source['sha256'],source_run_id=str(source['run_id']),
                    source_context_sha256=source['context_sha256'],review_evidence_id=str(source['evidence_id']),
                    review_evidence_sha256=digest({'result':source['review'],'input_refs':source['review_refs'],'verifier_version':source['review_verifier']}),
                    review_decision='SYNTHETIC_FIXTURE' if request.synthetic_fixture else source['review']['decision'],input_relation='USER_SUPPLIED_NOT_ASSERTED_IDENTICAL')
        case_id=uid();case_hash=digest(case)
        c.execute(text('''INSERT INTO text_regression_cases(case_id,task_id,source_artifact_id,review_evidence_id,snapshot,snapshot_sha256)
            VALUES (:id,:task,:artifact,:review,CAST(:snapshot AS jsonb),:sha)'''),
            {'id':case_id,'task':task_id,'artifact':request.artifact_id,'review':source['evidence_id'],'snapshot':canonical(case),'sha':case_hash})
        kernel._event(c,'text_eval.case_created',case_id,{'request_sha256':request_hash,'snapshot_sha256':case_hash,'task_id':task_id},actor='user',key=key)
        return case_id,True



def _assert_case_source(c, task_id, row):
    case=row['snapshot']
    source=one(c,'''SELECT a.content,a.sha256,r.run_id,r.status AS run_status,w.task_id,w.task_version,
            ctx.manifest,ctx.sha256 AS context_sha256,e.result AS review,e.input_refs AS review_refs,e.verifier_version AS review_verifier
        FROM artifacts a JOIN runs r ON r.run_id=a.run_id JOIN work_items w ON w.work_id=r.work_id
        JOIN run_contexts ctx ON ctx.run_id=r.run_id
        JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type=:review_type
        WHERE a.artifact_id=CAST(:artifact AS uuid) AND e.evidence_id=CAST(:review AS uuid)''',
        {'artifact':case['source_artifact_id'],'review':case['review_evidence_id'],
         'review_type':'provided_text_structure' if case['review_decision']=='SYNTHETIC_FIXTURE' else 'human_work_review'})
    if (not source or not isinstance(source['content'],dict) or not isinstance(source['manifest'],dict)
        or not isinstance(source['review_refs'],dict) or not isinstance(source['review'],dict)
        or source['task_id']!=task_id or source['task_version']!=case['base_task_version']
        or str(source['run_id'])!=case['source_run_id'] or source['run_status']!='SUCCEEDED'
        or (source['review'].get('passed') is not True if case['review_decision']=='SYNTHETIC_FIXTURE' else source['review'].get('decision')!=case['review_decision'])
        or digest({'result':source['review'],'input_refs':source['review_refs'],'verifier_version':source['review_verifier']})!=case['review_evidence_sha256']
        or source['review_refs'].get('artifact_sha256')!=case['source_artifact_sha256']
        or source['sha256']!=case['source_artifact_sha256'] or digest(source['content'])!=source['sha256']
        or source['context_sha256']!=case['source_context_sha256'] or digest(source['manifest'])!=source['context_sha256']):
        raise Conflict('evaluation case source or review has changed')


def freeze_suite(db, task_id, request):
    ids=sorted(str(x) for x in request.case_ids)
    if len(set(ids)) != len(ids):
        raise ValueError('평가 사례가 중복되었습니다')
    request_hash=digest({'task_id':task_id,'case_ids':ids})
    key='text-suite:'+request.idempotency_key
    kernel=Kernel(db)
    with db.begin() as c:
        kernel._lock(c)
        previous=one(c,'SELECT subject_id,payload FROM work_events WHERE idempotency_key=:key',{'key':key})
        if previous:
            if previous['payload'].get('request_sha256')!=request_hash:
                raise Conflict('suite idempotency key already used')
            return previous['subject_id'],False
        prior=c.execute(text('SELECT * FROM text_regression_suites WHERE task_id=:task ORDER BY version'),{'task':task_id}).mappings().all()
        required=set()
        for suite in prior:
            required.update(x['case_id'] for x in verify_suite(c,task_id,suite)['cases'])
        if not required.issubset(ids):
            raise Conflict('new evaluation suite must retain all previously frozen cases')
        cases=[]
        for case_id in ids:
            row=one(c,'SELECT * FROM text_regression_cases WHERE case_id=CAST(:id AS uuid) AND task_id=:task',{'id':case_id,'task':task_id})
            if not row:
                raise NotFound('evaluation case not found for task')
            if digest(row['snapshot'])!=row['snapshot_sha256']:
                raise Conflict('evaluation case snapshot is corrupted')
            _assert_case_source(c,task_id,row)
            cases.append({'case_id':case_id,'case_sha256':row['snapshot_sha256'],'case':row['snapshot']})
        version=one(c,'SELECT COALESCE(MAX(version),0)+1 AS version FROM text_regression_suites WHERE task_id=:task',{'task':task_id})['version']
        snapshot={'task_id':task_id,'version':version,'cases':cases,'scope':'USER_DEFINED_SUBSTRING_CHECKS_NOT_SEMANTIC'}
        if any(x['case'].get('authoring_scope')=='SYNTHETIC_FIXTURE_ORACLE_NOT_HUMAN_QUALITY_LABEL' for x in cases):
            snapshot['scope']='SYNTHETIC_FIXTURE_ORACLES_NOT_HUMAN_QUALITY_LABELS'
        suite_id=uid();suite_hash=digest(snapshot)
        c.execute(text('''INSERT INTO text_regression_suites(suite_id,task_id,version,snapshot,snapshot_sha256)
            VALUES (:id,:task,:version,CAST(:snapshot AS jsonb),:sha)'''),{'id':suite_id,'task':task_id,'version':version,'snapshot':canonical(snapshot),'sha':suite_hash})
        kernel._event(c,'text_eval.suite_frozen',suite_id,{'request_sha256':request_hash,'snapshot_sha256':suite_hash,'task_id':task_id,'version':version},actor='user',key=key)
        return suite_id,True


def verify_suite(c, task_id, suite):
    if not suite or suite['task_id'] != task_id or digest(suite['snapshot']) != suite['snapshot_sha256']:
        raise Conflict('evaluation suite snapshot missing or corrupted')
    snapshot=suite['snapshot']
    if snapshot.get('task_id') != task_id or snapshot.get('version') != suite['version'] or not snapshot.get('cases'):
        raise Conflict('evaluation suite metadata is invalid')
    seen=set()
    for entry in snapshot['cases']:
        case_id=entry['case_id']
        if case_id in seen:
            raise Conflict('duplicate evaluation case')
        seen.add(case_id)
        row=one(c,'SELECT * FROM text_regression_cases WHERE case_id=CAST(:id AS uuid) AND task_id=:task',{'id':case_id,'task':task_id})
        if not row or row['snapshot_sha256']!=entry['case_sha256'] or digest(row['snapshot'])!=row['snapshot_sha256'] or row['snapshot']!=entry['case']:
            raise Conflict('evaluation case has changed')
        _assert_case_source(c,task_id,row)
    return snapshot
