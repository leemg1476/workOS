import json
from types import SimpleNamespace
import pytest
import openai
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.api.main import app
from app.worker import process_claim
from app.integrations.report_research import ReportExecutionOptions, request_options


def test_bounded_research_and_ai_feedback_never_approve_work(kernel,monkeypatch):
    from test_task_memory import register,trigger
    from app.work.register_design import register as register_service, RegisterDesign
    from sqlalchemy import text
    with TestClient(app) as client:
        task=register(client,mode='LIVE_LLM')
        # Re-register the same draft with research is rejected rather than silently changing a task contract.
        with kernel.engine.connect() as c:
            row=c.execute(text('SELECT design_id,revision FROM registered_work_designs WHERE task_id=:id'),{'id':task}).mappings().one()
        from app.work.service import Conflict
        with pytest.raises(Conflict):
            register_service(kernel.engine,str(row['design_id']),RegisterDesign(revision=row['revision'],source_text='배송 지연 문의',model_mode='LIVE_LLM',execution_options=ReportExecutionOptions(web_search=True)))
        calls=[]
        class Responses:
            def create(self,**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(status='completed',output_text='이전 초안의 근거 부족을 보완한 결과',model='test-model',id='response',usage=None,output=[])
        monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=Responses()))
        monkeypatch.setenv('openai_api_key','test-key');monkeypatch.setenv('openai_model','test-model')
        work=trigger(client,task,'feedback1');run=kernel.claim();result=process_claim(kernel,run);assert result['passed']
        body={'artifact_id':result['artifact_id'],'note':'AI 의견: 근거와 수치 계산을 보완하세요.','evaluator_model':'test-judge',
              'rubric_version':'research-test-v1','metrics':{'score':40},'idempotency_key':'feedback'}
        url='/api/work/'+work+'/agent-feedback'
        assert client.post(url,json=body).json()['recorded']
        assert not client.post(url,json=body).json()['recorded']
        assert client.post(url,json={**body,'note':'다른 의견'}).status_code==409
        detail=client.get('/api/work/'+work).json()['work']
        assert detail['status']=='WAITING_APPROVAL' and detail['accepted_artifact_id'] is None
        trigger(client,task,'feedback2');run2=kernel.claim();assert process_claim(kernel,run2)['passed']
        memory=json.loads(calls[-1]['input'][1]['content'])['task_memory']
        assert memory['reviews']==[] and memory['unapplied_ai_reviews_omitted']==1
        assert client.get('/api/tasks/'+task+'/knowledge').json()[0]['decision']=='PROPOSED'
        assert memory['draft_results'][0]['scope']=='UNACCEPTED_DRAFT_NOT_VERIFIED_FACTS'
        assert memory['draft_results'][0]['excerpt'].count('이전 초안의 근거 부족을 보완한 결과')==3
        assert memory['accepted_results']==[]


def test_research_option_bounds_and_provenance():
    options=ReportExecutionOptions(web_search=True,allowed_domains=['korea.kr'],max_output_tokens=3000)
    args=request_options(options)
    assert args['tools'][0]['filters']=={'allowed_domains':['korea.kr']}
    assert args['include']==['web_search_call.action.sources']
    assert args['max_tool_calls']==2
    for bad in [{'max_output_tokens':100000},{'max_tool_calls':50},{'allowed_domains':['https://localhost:8000']}]:
        with pytest.raises(ValidationError): ReportExecutionOptions(**bad)
