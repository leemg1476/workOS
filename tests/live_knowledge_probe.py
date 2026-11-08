"""Explicit, two-call synthetic LIVE integration check; not a quality-score benchmark."""
import json
import os
from pathlib import Path
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.work.db import engine
from app.work.service import Kernel
from app.evaluation.longitudinal_report import configure
from app.api.main import app
from test_task_knowledge import register,propose,apply,run,EXPRESSION


if __name__=='__main__':
    if os.getenv('WORKOS_LIVE_VERIFY_CONFIRM')!='synthetic' or engine.url.database!='workos_knowledge_verify':
        raise SystemExit('Use the dedicated workos_knowledge_verify DB and explicitly enable synthetic verification')
    configure();out=Path('docs/verification/task-knowledge-20261108');out.mkdir(parents=True,exist_ok=True)
    kernel=Kernel(engine)
    with TestClient(app) as client:
        task=register(client,mode='LIVE_LLM',options={'max_output_tokens':3500,'timeout_seconds':150})
        calc=propose(client,task,'CALCULATION',{'expression':EXPRESSION,'unit':'원','decimal_places':2},'가상 A 가계 월 증가액')
        rule=propose(client,task,'CHECKLIST',{'heading':'결과 활용과 한계','instruction':'가상 고정수량의 근사 계산임을 밝히고 독자가 확인할 생활비 항목을 설명한다.'},'가정과 실행 조건')
        assert apply(client,calc,'fixture-calc').status_code==200
        assert apply(client,rule,'fixture-rule').status_code==200
        first,result=run(client,kernel,task,'first-live');assert result and result['passed']
        for n in range(12):
            r=client.post('/api/work/'+first['work_id']+'/agent-feedback',json={'artifact_id':result['artifact_id'],
                'note':'검증되지 않은 평가 의견 '+str(n),'evaluator_model':'synthetic-reviewer','rubric_version':'fixture-v1','idempotency_key':'noise-'+str(n)})
            assert r.status_code==200
        second,result=run(client,kernel,task,'second-live');assert result and result['passed']
        results=[]
        for claim in (first,second):
            detail=client.get('/api/runs/'+claim['run_id']).json()
            artifact=client.get('/api/runs/'+claim['run_id']+'/artifacts').json()[0]
            assert '50800.00 원' in artifact['content']['steps'][-1]['text']
            assert len(detail['context']['manifest']['prompt_memory']['manifest']['knowledge']['active'])==2
            assert detail['context']['manifest']['prompt_memory']['manifest']['reviews']==[]
            results.append({'run':detail,'artifact':artifact})
        proof={'scope':'TWO_LIVE_WRITER_CALLS_ON_SYNTHETIC_DATA_NOT_SEMANTIC_QUALITY_IMPROVEMENT',
            'approval_scope':'SIMULATED_USER_APPLY_IN_TEST_DATABASE_ONLY; REAL_STUDY_TASK_NOT_ACTIVATED',
            'task_id':task,'unrelated_ai_feedback_count':12,'expected_calculation_won':'50800.00',
            'applied_knowledge_retained':True,'unapproved_ai_opinions_excluded':True,'results':results}
        (out/'live-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
        for n,result in enumerate(results,1):
            (out/f'live-report-{n}.md').write_text(result['artifact']['content']['steps'][-1]['text'],encoding='utf-8')
        print(json.dumps({k:v for k,v in proof.items() if k!='results'},ensure_ascii=False),flush=True)
