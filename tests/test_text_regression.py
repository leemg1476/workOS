import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.api.main import app
from app.worker import process_claim
from app.work.db import engine
from app.work.market import digest


def create_reviewed(client, kernel, key):
    d=client.post('/api/work-designs',json={'description':'문의 유형 요약','mode':'MOCK'}).json()
    d['plan']['expected_result']='유형별 결과';d['plan']['evaluation_criteria']=['요약에 배송을 포함']
    assert client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':d['plan']}).status_code==200
    task=client.post('/api/work-designs/'+d['design_id']+'/register',json={'revision':2,'source_text':'원본 가상 입력','model_mode':'MOCK'}).json()['task_id']
    work=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':key,'input_snapshot':{}}).json()['work_id']
    claim=kernel.claim();result=process_claim(kernel,claim)
    assert result['passed']
    return task,work,result['artifact_id']


def test_feedback_case_and_immutable_suite(kernel):
    with TestClient(app) as client:
        task,work,artifact=create_reviewed(client,kernel,'case-work')
        endpoint='/api/tasks/'+task+'/regression-cases'
        body={'artifact_id':artifact,'step_id':'collect','input_text':'익명 사례: 배송 지연','expected_behavior':'배송 문제 언급',
              'feedback':'배송 문제 누락','required_phrases':['배송'],'forbidden_phrases':['환불 완료'],'idempotency_key':'case-1'}
        assert client.post(endpoint,json=body).status_code==409
        review={'artifact_id':artifact,'decision':'REJECT','note':'요약이 충분하지 않음','idempotency_key':'review-case'}
        assert client.post('/api/work/'+work+'/review',json=review).status_code==200
        assert client.post(endpoint,json={**body,'input_text':'사용자 a@example.com'}).status_code==422
        assert client.post(endpoint,json={**body,'required_phrases':[],'forbidden_phrases':[]}).status_code==422
        assert client.post(endpoint,json={**body,'forbidden_phrases':['배송']}).status_code==422
        assert client.post(endpoint,json={**body,'required_phrases':['배송 지연'],'forbidden_phrases':['배송']}).status_code==422
        assert client.post(endpoint,json={**body,'step_id':'missing'}).status_code==422
        case=client.post(endpoint,json=body);assert case.status_code==200 and case.json()['created']
        case_id=case.json()['case_id']
        assert client.post(endpoint,json=body).json()=={'case_id':case_id,'created':False}
        assert client.post(endpoint,json={**body,'feedback':'다른 요청'}).status_code==409
        recorded=client.get(endpoint).json()[0]
        assert recorded['snapshot']['input_text']=='익명 사례: 배송 지연'
        assert recorded['snapshot']['input_relation']=='USER_SUPPLIED_NOT_ASSERTED_IDENTICAL'
        assert recorded['snapshot']['review_decision']=='REJECT'
        assert '원본 가상 입력' not in str(recorded['snapshot'])
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text("UPDATE text_regression_cases SET snapshot='{}' WHERE case_id=:id"),{'id':case_id})
        suite_path='/api/tasks/'+task+'/regression-suites'
        request={'case_ids':[case_id],'idempotency_key':'suite-1'}
        frozen=client.post(suite_path,json=request);assert frozen.status_code==200 and frozen.json()['created']
        assert client.post(suite_path,json=request).json()['created'] is False
        suite=client.get(suite_path).json()[0]
        assert suite['version']==1 and suite['snapshot']['cases'][0]['case_sha256']==recorded['snapshot_sha256']
        assert 'NOT_SEMANTIC' in suite['snapshot']['scope']
        assert client.post(suite_path,json={'case_ids':[case_id,case_id],'idempotency_key':'suite-dupe'}).status_code==422
        assert client.post('/api/tasks/OTHER/regression-suites',json={'case_ids':[case_id],'idempotency_key':'other-task'}).status_code==404
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text("DELETE FROM text_regression_suites WHERE suite_id=:id"),{'id':suite['suite_id']})
        with engine.begin() as c:
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{steps,0,text}',CAST('\"변조\"' AS jsonb)) WHERE artifact_id=:id"),{'id':artifact})
        assert client.post(suite_path,json={'case_ids':[case_id],'idempotency_key':'suite-after-source-change'}).status_code==409
        assert client.get(suite_path).json()[0]['snapshot_sha256']==suite['snapshot_sha256']


def test_case_refuses_corrupted_reviewed_artifact(kernel):
    with TestClient(app) as client:
        task,work,artifact=create_reviewed(client,kernel,'case-corrupt-work')
        assert client.post('/api/work/'+work+'/review',json={'artifact_id':artifact,'decision':'REJECT','note':'불만','idempotency_key':'case-corrupt-review'}).status_code==200
        with engine.begin() as c:
            changed=c.execute(text("SELECT jsonb_set(content,'{steps,0,text}',CAST('\"변조\"' AS jsonb)) FROM artifacts WHERE artifact_id=:id"),{'id':artifact}).scalar_one()
            c.execute(text("UPDATE artifacts SET content=CAST(:content AS jsonb),sha256=:sha WHERE artifact_id=:id"),
                {'content':json.dumps(changed,ensure_ascii=False),'sha':digest(changed),'id':artifact})
        request={'artifact_id':artifact,'step_id':'collect','input_text':'익명 문의','expected_behavior':'올바른 요약','feedback':'내용 부정확',
                 'required_phrases':['문의'],'forbidden_phrases':[],'idempotency_key':'corrupt-case'}
        assert client.post('/api/tasks/'+task+'/regression-cases',json=request).status_code==409


def test_suite_refuses_changed_review_evidence(kernel):
    with TestClient(app) as client:
        task,work,artifact=create_reviewed(client,kernel,'changed-review-work')
        assert client.post('/api/work/'+work+'/review',json={'artifact_id':artifact,'decision':'REJECT','note':'요약 누락','idempotency_key':'changed-review'}).status_code==200
        request={'artifact_id':artifact,'step_id':'collect','input_text':'익명 문의','expected_behavior':'정확한 요약','feedback':'누락',
                 'required_phrases':['문의'],'forbidden_phrases':[],'idempotency_key':'changed-review-case'}
        case=client.post('/api/tasks/'+task+'/regression-cases',json=request).json()
        with engine.begin() as c:
            c.execute(text("UPDATE evidence SET result=jsonb_set(result,'{note}',CAST('\"변조된 판단\"' AS jsonb)) WHERE artifact_id=:id AND verification_type='human_work_review'"),{'id':artifact})
        response=client.post('/api/tasks/'+task+'/regression-suites',json={'case_ids':[case['case_id']],'idempotency_key':'changed-review-suite'})
        assert response.status_code==409
