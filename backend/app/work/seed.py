import hashlib
import json
from sqlalchemy import text
from app.domain.contracts import MarketBriefing, PolicyImpact, VocReport, VocReportWithHistory
from app.work.market import digest
from app.work.voc import METHODS, fixture_records, fixture_cases, fixture_regression_cases

TASKS = [
    ('TASK-001', '일일 금융시장 리스크 브리핑', '시장 지표와 출처를 검증한 브리핑', 'CRON', '0 8 * * 1-5', None),
    ('TASK-002', '내부 규정 변경 영향 분석', '문서 변경의 영향과 후속 작업 분석', 'EVENT', None, 'document.updated'),
    ('TASK-003', '고객 VOC 개선과제 추천', 'VOC 근거 기반 개선과제 추천', 'CRON', '0 9 * * 1', None),
]


def seed(engine):
    with engine.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(947203)'))
        for task_id, name, objective, trigger, schedule, event in TASKS:
            c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
                VALUES (:id,1,:name,:objective,'{}','{}','{"kind":"not_implemented"}',true) ON CONFLICT DO NOTHING'''),
                {'id': task_id, 'name': name, 'objective': objective})
            c.execute(text('''INSERT INTO job_definitions(job_id,task_id,task_version,trigger_type,schedule,event_filter)
                VALUES (:job,:task,1,:trigger,:schedule,:event) ON CONFLICT DO NOTHING'''),
                {'job': task_id+'-JOB', 'task': task_id, 'trigger': trigger, 'schedule': schedule, 'event': event})
        c.execute(text("UPDATE task_definitions SET active=false WHERE task_id='TASK-001' AND version=1 AND active"))
        c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
            VALUES ('TASK-001',2,'일일 금융시장 리스크 브리핑','가상 지표의 수치와 출처를 검증한 브리핑',
                    CAST(:input AS jsonb),CAST(:output AS jsonb),'{"kind":"market_briefing_v1"}',true)
            ON CONFLICT(task_id,version) DO NOTHING'''), {
            'input': json.dumps({'dataset_version':['v1','v2'], 'model_mode':['MOCK','LIVE_LLM']}, ensure_ascii=False),
            'output': json.dumps(MarketBriefing.model_json_schema(), ensure_ascii=False)})
        c.execute(text("UPDATE job_definitions SET task_version=2 WHERE job_id='TASK-001-JOB' AND task_version=1"))
        c.execute(text("UPDATE task_definitions SET active=false WHERE task_id='TASK-002' AND version=1 AND active"))
        c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
            VALUES ('TASK-002',2,'내부 규정 변경 영향 분석','가상 정책 변경에 따른 영향과 후속 작업을 추적',
                    CAST(:input AS jsonb),CAST(:output AS jsonb),'{"kind":"policy_impact_v1"}',true)
            ON CONFLICT(task_id,version) DO NOTHING'''), {
            'input': json.dumps({'event_type':'document.updated'}, ensure_ascii=False),
            'output': json.dumps(PolicyImpact.model_json_schema(), ensure_ascii=False)})
        c.execute(text("UPDATE job_definitions SET task_version=2 WHERE job_id='TASK-002-JOB' AND task_version=1"))
        c.execute(text("UPDATE task_definitions SET active=false WHERE task_id='TASK-003' AND version=1 AND active"))
        c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
            VALUES ('TASK-003',2,'고객 VOC 개선과제 추천','가상 VOC 근거에 기반한 분류와 개선 추천',
                    CAST(:input AS jsonb),CAST(:output AS jsonb),'{"kind":"voc_report_v1"}',true)
            ON CONFLICT(task_id,version) DO NOTHING'''), {
            'input': json.dumps({'source_version':'voc-demo-v1'},ensure_ascii=False),
            'output': json.dumps(VocReport.model_json_schema(),ensure_ascii=False)})
        c.execute(text("UPDATE job_definitions SET task_version=2 WHERE job_id='TASK-003-JOB' AND task_version=1"))
        c.execute(text("UPDATE task_definitions SET active=false WHERE task_id='TASK-003' AND version=2 AND active"))
        c.execute(text('''INSERT INTO task_definitions(task_id,version,name,objective,input_schema,output_schema,acceptance_policy,active)
            VALUES ('TASK-003',3,'고객 VOC 개선과제 추천','가상 VOC 근거와 이전 추천 범주를 비교한 개선 추천',
                    CAST(:input AS jsonb),CAST(:output AS jsonb),'{"kind":"voc_report_v2"}',true)
            ON CONFLICT(task_id,version) DO NOTHING'''), {
            'input':json.dumps({'source_version':'voc-demo-v1','history':'previous_verified_report'},ensure_ascii=False),
            'output':json.dumps(VocReportWithHistory.model_json_schema(),ensure_ascii=False)})
        c.execute(text("UPDATE job_definitions SET task_version=3 WHERE job_id='TASK-003-JOB' AND task_version=2"))
        for method_id,rules in METHODS.items():
            c.execute(text('''INSERT INTO voc_method_versions(method_id,changed_component,rules,sha256)
                VALUES (:id,'classification_rules',CAST(:rules AS jsonb),:sha) ON CONFLICT DO NOTHING'''),
                {'id':method_id,'rules':json.dumps(rules,ensure_ascii=False),'sha':digest(rules)})
        c.execute(text("INSERT INTO voc_method_state(task_id,active_method_id) VALUES ('TASK-003','champion-v1') ON CONFLICT DO NOTHING"))
        records = fixture_records()
        for row in records:
            c.execute(text('INSERT INTO voc_records(record_id,source_version,body) VALUES (:record_id,:source_version,:body) ON CONFLICT DO NOTHING'),row)
        cases = fixture_cases()
        c.execute(text("INSERT INTO voc_eval_suites(suite_version,case_count,sha256) VALUES ('voc-suite-v1',:n,:sha) ON CONFLICT DO NOTHING"),
                  {'n':len(cases),'sha':digest(cases)})
        for case in cases:
            c.execute(text("INSERT INTO voc_eval_cases(suite_version,case_id,body,expected_category) VALUES ('voc-suite-v1',:case_id,:body,:expected_category) ON CONFLICT DO NOTHING"),case)
        regression = fixture_regression_cases()
        c.execute(text("INSERT INTO voc_eval_suites(suite_version,case_count,sha256) VALUES ('voc-regression-v1',:n,:sha) ON CONFLICT DO NOTHING"),
                  {'n':len(regression),'sha':digest(regression)})
        for case in regression:
            c.execute(text("INSERT INTO voc_eval_cases(suite_version,case_id,body,expected_category) VALUES ('voc-regression-v1',:case_id,:body,:expected_category) ON CONFLICT DO NOTHING"),case)
        policy_versions = [
            (1, {'retention':'익명 VOC 자료는 90일 보관한다.', 'review':'VOC 분류 기준은 매월 검토한다.'}),
            (2, {'retention':'익명 VOC 자료는 30일 보관한다.', 'review':'VOC 분류 기준은 매주 검토한다.'}),
        ]
        for version, clauses in policy_versions:
            c.execute(text('''INSERT INTO policy_documents(document_id,version,title,clauses,sha256)
                VALUES ('POLICY-VOC',:v,'가상 VOC 운영 규정',CAST(:clauses AS jsonb),:sha) ON CONFLICT DO NOTHING'''),
                {'v':version,'clauses':json.dumps(clauses,ensure_ascii=False),'sha':digest(clauses)})
        c.execute(text('''INSERT INTO task_source_dependencies(task_id,document_id,pinned_version,status)
            VALUES ('TASK-003','POLICY-VOC',1,'CURRENT') ON CONFLICT DO NOTHING'''))
        market = [('v1','USD/KRW','KRW','1340','1350'),('v1','KOSPI','index','2600','2626'),
                  ('v2','USD/KRW','KRW','1340','1380'),('v2','KOSPI','index','2600','2626')]
        for version,symbol,unit,previous,current in market:
            c.execute(text('''INSERT INTO market_observations(dataset_version,symbol,unit,previous_value,current_value,observed_at)
                VALUES (:v,:symbol,:unit,:previous,:current,'2026-10-30T07:00:00+09:00') ON CONFLICT DO NOTHING'''),
                {'v':version,'symbol':symbol,'unit':unit,'previous':previous,'current':current})
        documents = [('DOC-FX',1,'가상 환율 동향','데모 문서: 달러 환율 변동에 대한 가상의 시장 관찰이다.'),
                     ('DOC-RISK',1,'가상 시장 리스크','데모 문서: 주가와 환율의 동시 변화를 검토한다.')]
        for doc,version,title,body in documents:
            body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
            c.execute(text('''INSERT INTO source_documents(document_id,version,title,body,sha256)
                VALUES (:id,:v,:title,:body,:sha) ON CONFLICT DO NOTHING'''),
                {'id':doc,'v':version,'title':title,'body':body,'sha':body_hash})
