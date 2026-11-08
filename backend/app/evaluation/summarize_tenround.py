"""Export observed evidence only; prose observations are written by the operator."""
import json
import re
from pathlib import Path
from sqlalchemy import text
from app.work.db import engine
from app.domain.hashing import digest
from app.evaluation.longitudinal_report import reference

OUT=Path('docs/experiments/household-tenround-20261108')
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def main():
    if engine.url.database!='workos_tenround_20261108':raise RuntimeError('dedicated experiment DB required')
    state=load(OUT/'study.json');summary=[]
    expected=['50800.00 원','2.54 %','8800.00 원','46400.00 원','2.32 %','4400.00 원','50000.00 원']
    with engine.connect() as c:
        attempts=[dict(r) for r in c.execute(text('''SELECT r.run_id,r.work_id,r.attempt_no,r.status,r.error,r.started_at,r.finished_at,
            a.artifact_id,a.sha256 AS artifact_sha256,a.content FROM runs r LEFT JOIN artifacts a USING(run_id)
            JOIN work_items w USING(work_id) WHERE w.task_id=:task ORDER BY r.started_at'''),{'task':state['task_id']}).mappings()]
        save(OUT/'attempts.json',attempts)
        for folder in sorted(OUT.glob('round-*')):
            if not (folder/'grade.json').exists():continue
            r=load(folder/'report.json');g=load(folder/'grade.json');review=load(folder/'operator_review.json') if (folder/'operator_review.json').exists() else None
            content=r['content'];body=content['steps'][-1]['text'];memory=r['memory']['manifest'];active=memory['knowledge']['active']
            evidence=[dict(row) for row in c.execute(text('SELECT verification_type,verifier_version,result FROM evidence WHERE artifact_id=:id'),{'id':r['artifact_id']}).mappings()]
            snapshots=c.execute(text('SELECT count(*) FROM report_source_snapshots WHERE run_id=:id'),{'id':r['run_id']}).scalar_one()
            context=c.execute(text('SELECT manifest FROM run_contexts WHERE run_id=:id'),{'id':r['run_id']}).scalar_one()
            summary.append({'round':r['round'],'score':g['score'],'ratings':g['ratings'],'judge_critical_errors':g['critical_errors'],
                'operator_review':review,'chars':len(body),'active_knowledge':len(active),'lessons':sum(e['kind']=='LESSON' for e in active),
                'active_entry_ids':[e['entry_id'] for e in active], 'canonical_table_all_values':all(v in body for v in expected),
                'duplicate_heading':bool(re.search(r'## ([^\n]+)\n\n### \1',body)),
                'captured_sources_now':snapshots,'readbacks_as_generated':content['steps'][-1].get('research',{}).get('source_snapshots',[]),
                'source_sha256':context['source_sha256'],'contract_sha256':context['contract_sha256'],
                'prompt_memory_reviews':context['prompt_memory']['manifest']['reviews'],
                'web_calls':len(content['steps'][-1].get('research',{}).get('web_calls',[])),
                'artifact_hash_matches':digest(content)==r['artifact_sha256'],'run_id':r['run_id'],'work_id':r['work_id'],
                'artifact_id':r['artifact_id'],'artifact_sha256':r['artifact_sha256'],'memory_sha256':r['memory']['sha256'],'evidence':evidence})
    save(OUT/'summary.json',{'scope':'SINGLE_ADAPTIVE_SEQUENCE_WITH_CODEX_REVIEW_NOT_CAUSAL_QUALITY_PROOF','rounds':summary,
        'completed':len(summary),'total_runs':len(attempts),'failed_runs':sum(r['status'] not in ('SUCCEEDED','RUNNING') for r in attempts),'reference':reference()})
    print(json.dumps([{'round':r['round'],'score':r['score'],'knowledge':r['active_knowledge'],
        'verified':any('passed' in e['result'] for e in r['evidence']) and all(e['result']['passed'] for e in r['evidence'] if 'passed' in e['result'])} for r in summary]))

if __name__=='__main__':main()
