"""Operator-reviewed experiment actions; never generate or auto-approve feedback."""
import argparse
import json
from pathlib import Path
from sqlalchemy import create_engine,text
from app.work.db import engine
from app.work.knowledge import propose,decide,entries,KnowledgeProposal,KnowledgeDecision
from app.integrations.source_snapshots import capture

OUT=Path('docs/experiments/household-tenround-20261108')
OLD='USER-b884a865-8872-40f6-ba63-4883c541522c'

def load(path):return json.loads(path.read_text(encoding='utf-8'))
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def apply(task,kind,title,payload,key,artifact=None):
    entry=propose(engine,task,KnowledgeProposal(kind=kind,title=title,payload=payload,origin_artifact_id=artifact,idempotency_key=key))
    with engine.connect() as c:row=next(r for r in entries(c,task) if str(r['entry_id'])==entry)
    decide(engine,entry,KnowledgeDecision(decision='APPLY',expected_sha256=row['sha256'],
        note='사용자 명시적 실험 전용 적용 승인에 따른 Codex 근거 검토. 운영 태스크로 승격하지 않음.',idempotency_key=key+':apply'))
    return entry

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bootstrap',action='store_true');parser.add_argument('--review',type=int);args=parser.parse_args()
    if engine.url.database!='workos_tenround_20261108':raise RuntimeError('dedicated experiment DB required')
    state=load(OUT/'study.json');task=state['task_id']
    if args.bootstrap:
        old=create_engine('postgresql+psycopg://workos:workos@127.0.0.1:5547/workos_memory_preview')
        with old.connect() as c:rows=[r for r in entries(c,OLD) if r['kind'] in ('CALCULATION','CHECKLIST')]
        assert len(rows)==10
        ids=[apply(task,r['kind'],r['title'],r['payload'],task+':seed:'+str(i)) for i,r in enumerate(rows)]
        save(OUT/'initial_knowledge.json',{'entry_ids':ids,'scope':'USER_AUTHORIZED_EXPERIMENT_ONLY','count':len(ids)})
    if args.review:
        n=args.review;folder=OUT/f'round-{n:02d}';report=load(folder/'report.json');review=load(folder/'operator_notes.json')
        assert review['artifact_sha256']==report['artifact_sha256'] and review['reviewed'] is True
        ids=[]
        if n==1:
            old=create_engine('postgresql+psycopg://workos:workos@127.0.0.1:5547/workos_memory_preview')
            with old.connect() as c:
                original=next(r for r in entries(c,OLD) if r['kind']=='SOURCE_EXCERPT')
                url=c.execute(text('SELECT url FROM report_source_snapshots WHERE snapshot_id=:id'),{'id':original['payload']['snapshot_id']}).scalar_one()
            snapshots=capture(engine,report['run_id'],'',{'citations':[{'url':url}]},['korea.kr'])
            save(folder/'operator_source_readback.json',snapshots)
            if snapshots[0]['status']!='CAPTURED':raise RuntimeError('source unavailable; cannot activate quote')
            ids.append(apply(task,'SOURCE_EXCERPT',original['title'],{'snapshot_id':snapshots[0]['snapshot_id'],'quote':original['payload']['quote']},task+':quote'))
        if n<10 and review.get('lesson'):
            kind=review.get('knowledge_kind','LESSON')
            if kind not in ('LESSON','CHECKLIST','CALCULATION'):raise ValueError('unsupported operator review kind')
            payload=({'text':review['lesson']} if kind=='LESSON' else
                {'heading':review['heading'],'instruction':review['lesson']} if kind=='CHECKLIST' else review['calculation_payload'])
            ids.append(apply(task,kind,f'검토 교훈 {n:02d}',payload,task+f':operator:{n}',report['artifact_id']))
        review['applied_entry_ids']=ids
        review['authorization']='USER_EXPLICIT_EXPERIMENT_ONLY_APPROVAL; CODEX_REVIEW_NOT_HUMAN_EXPERT'
        save(folder/'operator_review.json',review)
        print(json.dumps({'round':n,'applied_entries':ids},ensure_ascii=False))

if __name__=='__main__':main()
