from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine


def test_design_creation_revision_and_cycle_rejection(kernel):
    with TestClient(app) as client:
        created=client.post('/api/work-designs',json={'description':'매주 고객 문의를 정리한다','mode':'MOCK'})
        assert created.status_code==200
        data=created.json()
        assert data['status']=='DRAFT' and data['origin']=='MOCK'
        design_id=data['design_id']; plan=data['plan']
        assert len(plan['steps'])==3 and plan['steps'][1]['depends_on']==['collect']
        plan['expected_result']='문의 유형별 요약'
        edited=client.put(f'/api/work-designs/{design_id}',json={'revision':1,'plan':plan})
        assert edited.status_code==200 and edited.json()['revision']==2
        assert client.put(f'/api/work-designs/{design_id}',json={'revision':1,'plan':plan}).status_code==409
        plan['steps'][0]['depends_on']=['review']
        assert client.put(f'/api/work-designs/{design_id}',json={'revision':2,'plan':plan}).status_code==422
        latest=next(x for x in client.get('/api/work-designs').json() if x['design_id']==design_id)
        assert latest['revision']==2 and latest['plan']['expected_result']=='문의 유형별 요약'
        with engine.connect() as c:
            rows=c.execute(text('SELECT revision,origin FROM work_design_revisions WHERE design_id=:id ORDER BY revision'),{'id':design_id}).all()
            assert rows==[(1,'MOCK'),(2,'HUMAN_EDIT')]


def test_live_mode_does_not_fall_back(kernel,monkeypatch):
    for name in ('openai_api_key','OPENAI_API_KEY','openai_model','OPENAI_MODEL'):
        monkeypatch.delenv(name,raising=False)
    with TestClient(app) as client:
        result=client.post('/api/work-designs',json={'description':'업무 설명','mode':'LIVE_LLM'})
        assert result.status_code==422


def test_feedback_creates_new_draft_and_retains_input(kernel, monkeypatch):
    from app.work.designs import WorkPlan
    def fake_feedback(plan, feedback):
        plan['expected_result'] = '변경된 결과'
        return WorkPlan.model_validate(plan), {'origin':'HUMAN_FEEDBACK_LLM','model_id':'test-model','response_id':'test-response'}
    monkeypatch.setattr('app.api.main.apply_feedback',fake_feedback)
    with TestClient(app) as client:
        initial = client.post('/api/work-designs',json={'description':'문의 정리','mode':'MOCK'}).json()
        path = '/api/work-designs/'+initial['design_id']+'/feedback'
        response = client.post(path,json={'revision':1,'feedback':'기대 결과를 더 구체화'})
        assert response.status_code == 200 and response.json()['revision'] == 2
        assert client.post(path,json={'revision':1,'feedback':'재시도'}).status_code == 409
        latest=next(x for x in client.get('/api/work-designs').json() if x['design_id']==initial['design_id'])
        assert latest['plan']['expected_result']=='변경된 결과'
        assert latest['feedback']=='기대 결과를 더 구체화' and latest['origin']=='HUMAN_FEEDBACK_LLM'
        history=client.get('/api/work-designs/'+initial['design_id']+'/revisions').json()
        assert [x['revision'] for x in history]==[2,1]
        assert history[1]['plan']['expected_result'] != history[0]['plan']['expected_result']
