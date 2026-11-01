"""Exploratory recovery on the already-seen 24-minute task; not held-out."""
import json, os, time
from pathlib import Path
import run as prior
ROOT=Path(__file__).resolve().parents[2]
def main():
 if not os.getenv('DATABASE_URL','').endswith('/hcast_history_poc'):raise RuntimeError('isolated db required')
 root=Path(os.environ['HCAST_ROOT']);tex=Path(os.environ['HCAST_TEX_ASSETS']);_,chunks=prior.corpus(root,tex);chunks={(z['path'],z['offset']):z for z in chunks}
 previous=json.loads((ROOT/'docs/evidence/hcast_history/run.json').read_text(encoding='utf-8'));candidate=json.loads((ROOT/'docs/evidence/hcast_history/candidate.json').read_text(encoding='utf-8'));t=next(x for x in previous['tasks'] if x['task_key']=='local_research_tex/ar_report');plan=candidate['candidate_plan'];mod=prior.module(root/'local_research_tex/local_research_tex.py').TaskFamily;truth=mod.get_tasks()['ar_report']
 from fastapi.testclient import TestClient
 from sqlalchemy import text
 from app.api.main import app
 from app.work.db import engine
 from app.work.service import Kernel
 from app.worker import process_claim
 from app.domain.hashing import digest
 with TestClient(app) as client:
  with engine.connect() as c:
   rows=c.execute(text('''SELECT h.seq,h.task_key,h.question,h.prediction,h.expected,h.score,h.work_id::text,h.run_id::text,h.artifact_id::text,h.artifact_sha256,a.sha256,a.content FROM hcast_feedback_observations h JOIN artifacts a ON a.artifact_id=h.artifact_id ORDER BY h.seq''')).mappings().all()
  for row in rows:
   if row['sha256']!=row['artifact_sha256'] or digest(row['content'])!=row['sha256']:raise RuntimeError('invalid history')
  selected=[{k:h[k] for k in ('seq','task_key','question','prediction','expected','score','work_id','run_id','artifact_id','artifact_sha256')} for h in rows][-4:]
  refs=t['retrieval_refs'];ps=[chunks[(r['path'],r['offset'])] for r in refs]
  if any(r['sha256']!=p['sha256'] for r,p in zip(refs,ps)):raise RuntimeError('corpus mismatch')
  content='QUESTION: '+t['question']+'\n\nEXCERPTS:\n'+'\n\n'.join(f"SOURCE {z['path']} @{z['offset']}: {z['text']}" for z in ps)+'\n\nPREVIOUS SCORED WORK (different questions):\n'+json.dumps(selected,ensure_ascii=False,separators=(',',':'))
  def post(path,data):
   r=client.post(path,json=data)
   if r.status_code!=200:raise RuntimeError(r.text[:300])
   return r.json()
  d=post('/api/work-designs',{'description':'Recover previously failed HCAST research work','mode':'MOCK'});r=client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':plan})
  if r.status_code!=200:raise RuntimeError(r.text)
  reg=post('/api/work-designs/'+d['design_id']+'/register',{'revision':2,'source_text':content,'model_mode':'LIVE_LLM','schedule':''});task=reg['task_id'];t0=time.monotonic();tr=post(f'/api/tasks/{task}/run',{'idempotency_key':'ar-recovery-v1','input_snapshot':{}});kernel=Kernel(engine);claim=kernel.claim();outcome=process_claim(kernel,claim);elapsed=round((time.monotonic()-t0)*1000)
  w=client.get(f"/api/work/{tr['work_id']}").json();inf=client.get(f"/api/runs/{claim['run_id']}").json();arts=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()
  result={'schema':'hcast-recovery-exploratory-v1','warning':'This is a post-hoc reattempt of a previously executed task, not independent holdout or human productivity evidence.','task_key':t['task_key'],'human_baseline_minutes':24,'history_before':len(rows),'history_used':selected,'retrieval_refs':refs,'input_sha256':prior.sha(content),'task_id':task,'work_id':tr['work_id'],'run_id':str(claim['run_id']),'wall_ms':elapsed,'work_status':w['work']['status'],'run_status':inf['status'],'run_error':inf['error'],'outcome':outcome,'prediction':None,'score':0}
  if arts:
   a=arts[0];s=a['content']['steps'][0];man=inf['context']['manifest'];prediction=s['text'].strip();result.update(prediction=prediction,score=mod.score(truth,prediction),artifact_id=a['artifact_id'],artifact_sha256=a['sha256'],artifact_content=a['content'],evidence=a['evidence'],context_sha256=inf['context']['sha256'],source_sha256=man['source_sha256'],contract_sha256=man['contract_sha256'],model=s['model_id'],response_id=s['response_id'],input_tokens=s['input_tokens'],output_tokens=s['output_tokens'])
  (ROOT/'docs/evidence/hcast_history/recovery.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8');print('recovery',result['run_status'],result['work_status'],'score',result['score'],'wall_ms',elapsed,flush=True)
if __name__=='__main__':main()
