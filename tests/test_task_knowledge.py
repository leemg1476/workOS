import json
from types import SimpleNamespace
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.api.main import app
from app.worker import process_claim
from app.work.knowledge import calculate,reconcile_sources
from app.domain.hashing import digest


SOURCE={'food':800000,'other':1200000,'food_rate':3.2,'other_rate':2.1}
EXPRESSION={'op':'add','args':[
    {'op':'multiply','args':[{'path':'/food'},{'path':'/food_rate'},{'value':'0.01'}]},
    {'op':'multiply','args':[{'path':'/other'},{'path':'/other_rate'},{'value':'0.01'}]}]}


def register(client,mode='MOCK',web=False,options=None):
    d=client.post('/api/work-designs',json={'description':'가계 보고서 검증','mode':'MOCK'}).json()
    plan=d['plan'];plan.update(expected_result='가구 비교 보고서',evaluation_criteria=['계산과 출처 확인'],steps=[{'id':'report','title':'보고서','purpose':'가구 비교','depends_on':[]}])
    client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':plan})
    body={'revision':2,'source_text':json.dumps(SOURCE),'model_mode':mode}
    if web:body['execution_options']={'web_search':True,'allowed_domains':['korea.kr']}
    if options:body['execution_options']=options
    return client.post('/api/work-designs/'+d['design_id']+'/register',json=body).json()['task_id']


def run(client,kernel,task,key):
    result=client.post('/api/tasks/'+task+'/run',json={'idempotency_key':key,'input_snapshot':{}});assert result.status_code==200
    claim=kernel.claim();assert claim['work_id']==result.json()['work_id']
    return claim,process_claim(kernel,claim)


def propose(client,task,kind,payload,key):
    r=client.post('/api/tasks/'+task+'/knowledge',json={'kind':kind,'title':key,'payload':payload,'idempotency_key':key})
    assert r.status_code==200,r.text
    return next(e for e in client.get('/api/tasks/'+task+'/knowledge').json() if e['entry_id']==r.json()['entry_id'])


def apply(client,entry,key,decision='APPLY'):
    return client.post('/api/knowledge/'+entry['entry_id']+'/decision',json={'decision':decision,'expected_sha256':entry['sha256'],'note':'고정 근거를 확인한 테스트 사용자 결정','idempotency_key':key})


def test_approved_knowledge_survives_history_window_and_input_invalidation(kernel):
    with TestClient(app) as client:
        task=register(client);first,result=run(client,kernel,task,'first')
        lesson=propose(client,task,'LESSON',{'text':'전년비와 전월비는 다른 비교 기준이다.'},'keep-lesson')
        assert apply(client,lesson,'apply').status_code==200
        for n in range(12):
            response=client.post('/api/work/'+first['work_id']+'/agent-feedback',json={'artifact_id':result['artifact_id'],
              'note':'검증되지 않은 AI 의견 '+str(n),'evaluator_model':'test-reviewer','rubric_version':'test-v1','idempotency_key':str(n)})
            assert response.status_code==200
        entries=client.get('/api/tasks/'+task+'/knowledge').json()
        assert len(entries)==13 and sum(e['decision']=='APPLY' for e in entries)==1
        second,result=run(client,kernel,task,'second');assert result['passed']
        memory=client.get('/api/runs/'+second['run_id']).json()['task_memory']
        assert len(memory['manifest']['reviews'])==3
        assert memory['manifest']['knowledge']['active'][0]['entry_id']==lesson['entry_id']
        assert '검증되지 않은' not in json.dumps(memory['manifest']['knowledge'],ensure_ascii=False)
        assert client.put('/api/tasks/'+task+'/text-source',json={'source_text':'새 입력','expected_version':1,'idempotency_key':'new-source'}).status_code==200
        third,result=run(client,kernel,task,'third');assert result['passed']
        changed=client.get('/api/runs/'+third['run_id']).json()['task_memory']['manifest']['knowledge']
        assert changed['active']==[] and changed['excluded'][0]['reason']=='TASK_VERSION_OR_INPUT_CHANGED'
        assert client.get('/api/runs/'+second['run_id']).json()['task_memory']==memory
        with pytest.raises(DBAPIError),kernel.engine.begin() as c:
            c.execute(text('UPDATE task_knowledge SET title=\'tampered\' WHERE entry_id=CAST(:id AS uuid)'),{'id':lesson['entry_id']})


def test_missing_checklist_fails_and_decimal_table_is_not_generated_by_model(kernel,monkeypatch):
    returns=[]
    class Responses:
        def create(self,**kwargs):
            body=json.loads(kwargs['input'][1]['content']);knowledge=body['task_memory']['knowledge']['active']
            rules=[e for e in knowledge if e['kind']=='CHECKLIST']
            value={'introduction':'보고서는 계산표의 검증된 계산값을 참조합니다.','sections':[]}
            if returns:value['sections']=[{'rule_id':r['entry_id'],'text':'가구별 소비 비중과 확인할 행동을 비교합니다.'} for r in rules]
            returns.append(body)
            return SimpleNamespace(status='completed',output_text=json.dumps(value,ensure_ascii=False),model='test-model',id='test-response',usage=None,output=[])
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=Responses()))
    monkeypatch.setenv('openai_api_key','test-key');monkeypatch.setenv('openai_model','test-model')
    with TestClient(app) as client:
        task=register(client,mode='LIVE_LLM')
        calc=propose(client,task,'CALCULATION',{'expression':EXPRESSION,'unit':'원','decimal_places':2},'A 증가액')
        rule=propose(client,task,'CHECKLIST',{'instruction':'두 가구 차이를 설명하세요.','heading':'가구별 비교'},'comparison')
        assert calc['preview']['value']=='50800.00'
        assert apply(client,calc,'calc-apply').status_code==200
        assert apply(client,rule,'rule-apply').status_code==200
        first,result=run(client,kernel,task,'missing-rule')
        assert not result['passed'] and not result['result']['checks']['persistent_checklist_and_calculations']
        assert client.get('/api/work/'+first['work_id']).json()['work']['status']=='REWORK_REQUIRED'
        assert client.get('/api/runs/'+first['run_id']+'/artifacts').json()  # Failed candidate remains inspectable.
        second,result=run(client,kernel,task,'complete-rule');assert result['passed']
        report=client.get('/api/runs/'+second['run_id']+'/artifacts').json()[0]['content']['steps'][0]
        assert '50800.00 원' in report['text'] and '50800' not in report['structured_report']
        context=client.get('/api/runs/'+second['run_id']+'/review-context').json()
        assert context['knowledge']['active'] and 'ADVISORY' in context['review_policy']
        assert apply(client,rule,'retire',decision='RETIRE').status_code==200
        assert apply(client,rule,'retire',decision='RETIRE').json()['recorded'] is False
        wrong=client.post('/api/knowledge/'+calc['entry_id']+'/decision',json={'decision':'RETIRE','expected_sha256':'0'*64,'note':'stale','idempotency_key':'bad-sha'})
        assert wrong.status_code==409


def test_source_capture_reaches_reviewer_and_changed_quotes_invalidate_only_affected_work(kernel,monkeypatch):
    from app.integrations import source_snapshots
    text_value=['2025년 8월 일부 통신사의 한 달 50% 감면이 적용됐다.']
    monkeypatch.setattr(source_snapshots,'fetch_text',lambda url,domains:(text_value[0],'text/html'))
    class Responses:
        def create(self,**kwargs):return SimpleNamespace(status='completed',output_text='[공식 브리핑](https://www.korea.kr/example)',model='test-model',id='test-response',usage=None,output=[])
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=Responses()))
    monkeypatch.setenv('openai_api_key','test-key');monkeypatch.setenv('openai_model','test-model')
    with TestClient(app) as client:
        task=register(client,mode='LIVE_LLM',web=True)
        initial,result=run(client,kernel,task,'source-1');assert result['passed']
        ctx=client.get('/api/runs/'+initial['run_id']+'/review-context').json()
        snap=ctx['source_snapshots'][0];assert '50%' in snap['text']
        quote=propose(client,task,'SOURCE_EXCERPT',{'snapshot_id':snap['snapshot_id'],'quote':'한 달 50% 감면'},'source-quote')
        bad=propose(client,task,'SOURCE_EXCERPT',{'snapshot_id':snap['snapshot_id'],'quote':'2024년에 감면했다'},'unsupported-prior-year')
        assert apply(client,bad,'bad-proof').status_code==409
        assert apply(client,quote,'source-apply').status_code==200
        second,result=run(client,kernel,task,'source-2');assert result['passed']
        accept={'artifact_id':result['artifact_id'],'decision':'ACCEPT','note':'테스트 원문 검토 완료','idempotency_key':'accept'}
        assert client.post('/api/work/'+second['work_id']+'/review',json=accept).status_code==200
        third,result=run(client,kernel,task,'source-3');assert result['passed']
        text_value[0]='원문이 수정되어 기존 인용은 더 이상 없다.'
        fourth,result=run(client,kernel,task,'source-4')
        assert not result['passed'] and not result['result']['checks']['knowledge_source_quotes_current']
        assert client.get('/api/work/'+second['work_id']).json()['work']['status']=='STALE'
        assert client.post('/api/work/'+third['work_id']+'/review',json={**accept,'artifact_id':client.get('/api/runs/'+third['run_id']+'/artifacts').json()[0]['artifact_id'],'idempotency_key':'stale-accept'}).status_code==409
        memory=client.get('/api/tasks/'+task+'/memory').json()['memory']
        assert memory['knowledge']['active']==[] and memory['knowledge']['excluded']
        assert client.get('/api/work/'+initial['work_id']).json()['work']['status']=='WAITING_APPROVAL'
        assert client.put('/api/tasks/'+task+'/text-source',json={'source_text':json.dumps({**SOURCE,'food':999000}),'expected_version':1,'idempotency_key':'changed-input'}).status_code==200
        cross_input=propose(client,task,'SOURCE_EXCERPT',{'snapshot_id':snap['snapshot_id'],'quote':'한 달 50% 감면'},'old-snapshot-new-input')
        assert apply(client,cross_input,'cross-input-proof').status_code==409


def test_calculation_ast_rejects_code_and_nonfinite_values():
    assert str(calculate(EXPRESSION,SOURCE))=='50800.000'
    for expression in [{'value':'NaN'},{'value':True},{'op':'eval','args':[{'value':1},{'value':2}]},
                       {'op':'divide','args':[{'value':1},{'value':0}]},{'path':'__class__'}]:
        with pytest.raises((ValueError,ArithmeticError,KeyError)):calculate(expression,SOURCE)


def test_snapshot_adapter_rejects_private_addresses_and_preserves_no_script_text(monkeypatch):
    from app.integrations.source_snapshots import validate_url,VisibleText
    monkeypatch.setattr('socket.getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):validate_url('https://korea.kr/example',['korea.kr'])
    with pytest.raises(ValueError):validate_url('https://other.example/example',['korea.kr'])
    parser=VisibleText();parser.feed('<p>정상 원문</p><script>private();</script><style>bad{}</style>')
    assert '정상 원문' in ''.join(parser.parts) and 'private' not in ''.join(parser.parts)


def test_historical_import_only_creates_reviewable_candidates(kernel):
    from app.work.knowledge import import_feedback
    with TestClient(app) as client:
        task=register(client);claim,result=run(client,kernel,task,'history')
        body={'artifact_id':result['artifact_id'],'note':'과거 원문 교정 의견','evaluator_model':'test-reviewer','rubric_version':'test-v1','idempotency_key':'history'}
        assert client.post('/api/work/'+claim['work_id']+'/agent-feedback',json=body).status_code==200
        before=client.get('/api/tasks/'+task+'/knowledge').json()
        assert import_feedback(kernel.engine,task)==[before[0]['entry_id']]
        after=client.get('/api/tasks/'+task+'/knowledge').json()
        assert len(after)==1 and after[0]['decision']=='PROPOSED'
        assert client.get('/api/work/'+claim['work_id']).json()['work']['status']=='WAITING_APPROVAL'


def test_stopped_study_cannot_resume_or_make_model_calls(kernel,tmp_path,monkeypatch):
    from app.evaluation import longitudinal_report
    state=tmp_path/'study.json';state.write_text(json.dumps({'status':'STOPPED_AT_USER_REQUEST_FOR_INTERIM_REVIEW'}),encoding='utf-8')
    monkeypatch.setattr(longitudinal_report,'configure',lambda:None)
    monkeypatch.setenv('openai_model','test-model');monkeypatch.setenv('openai_api_key','test-key')
    class NoCalls:
        def __init__(self,**kwargs):pass
        @property
        def responses(self):raise AssertionError('stopped study must not call a model')
    monkeypatch.setattr(openai,'OpenAI',NoCalls)
    monkeypatch.setattr('sys.argv',['longitudinal_report','--output',str(tmp_path),'--rounds','50'])
    with pytest.raises(RuntimeError,match='study stopped by user'):longitudinal_report.main()
    assert len(list(tmp_path.iterdir()))==1
