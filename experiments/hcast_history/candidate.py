"""Exploratory prequential candidate evaluation following prior observed formatting failures."""
import json, os, time
from pathlib import Path
from copy import deepcopy
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import run as prior
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence/hcast_history/candidate.json'
TEST=['section_letter_theodore','clearer_section_letter_theodore','td_month','trpo']

def main():
 if not os.getenv('DATABASE_URL','').endswith('/hcast_history_poc'):raise RuntimeError('Isolated hcast_history_poc required')
 root=Path(os.environ['HCAST_ROOT']);tex=Path(os.environ['HCAST_TEX_ASSETS']);docs,chunks=prior.corpus(root,tex)
 base=json.loads((ROOT/'docs/evidence/hcast_history/run.json').read_text(encoding='utf-8'));plan=base['plan'];candidate=deepcopy(plan)
 candidate['steps'][0]['purpose'] += ' Your entire response must be only the requested exact English answer: no Hangul, explanation, answer prefix, punctuation, markdown or units. Use digits for numbers. For a list, separate names with comma and space, never “and”. Examples of formatting from previous evaluated work: 6 ; ARC Easy, ARC Challenge.'
 mod=prior.module(root/'local_research/local_research.py').TaskFamily
 v=TfidfVectorizer(stop_words='english',ngram_range=(1,2),sublinear_tf=True);matrix=v.fit_transform([x['text'] for x in chunks])
 from fastapi.testclient import TestClient
 from sqlalchemy import text
 from app.api.main import app
 from app.work.db import engine
 from app.work.service import Kernel
 from app.worker import process_claim
 from app.domain.hashing import digest
 result={'schema':'hcast-candidate-exploratory-v1','source_revision':prior.REV,'design':'Four previously unexecuted HCAST task variants. Both arms get identical source and last four previous scored Work records, frozen before the pair. Candidate modifies only the step purpose based on observed failures. Baseline result is appended only after both arms finish. Candidate is not promoted.', 'development_evidence':['run.json','followup.json'],'test_order':TEST,'baseline_plan':plan,'candidate_plan':candidate,'tasks':[]}
 def flush():OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
 with TestClient(app) as client:
  def post(path,data):
   r=client.post(path,json=data)
   if r.status_code!=200:raise RuntimeError(path+' '+r.text[:300])
   return r.json()
  def history():
   with engine.connect() as c:
    rows=c.execute(text('''SELECT h.seq,h.task_key,h.question,h.prediction,h.expected,h.score,h.work_id::text,h.run_id::text,h.artifact_id::text,h.artifact_sha256,a.sha256,a.content FROM hcast_feedback_observations h JOIN artifacts a ON a.artifact_id=h.artifact_id ORDER BY h.seq''')).mappings().all()
   for h in rows:
    if h['sha256']!=h['artifact_sha256'] or digest(h['content'])!=h['sha256']:raise RuntimeError('history changed')
   return [{k:h[k] for k in ('seq','task_key','question','prediction','expected','score','work_id','run_id','artifact_id','artifact_sha256')} for h in rows]
  with engine.connect() as c:
   if c.execute(text('SELECT count(*) FROM hcast_feedback_observations WHERE seq>=200')).scalar_one():raise RuntimeError('candidate run already recorded')
  versions={};tasks={};kernel=Kernel(engine)
  for arm,p in [('baseline',plan),('candidate',candidate)]:
   d=post('/api/work-designs',{'description':'HCAST scored historical record candidate '+arm,'mode':'MOCK'});resp=client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':p})
   if resp.status_code!=200:raise RuntimeError(resp.text)
   tasks[arm]={'design':d['design_id']}
  for i,key in enumerate(TEST):
   t=mod.get_tasks()[key];q=t['prompt'].strip();scores=cosine_similarity(v.transform([q]),matrix).ravel();picks=sorted(range(len(chunks)),key=lambda j:(-scores[j],j))[:4];ps=[chunks[j] for j in picks]
   refs=[{'path':z['path'],'offset':z['offset'],'sha256':z['sha256']} for z in ps];records=history();selected=records[-4:]
   content='QUESTION: '+q+'\n\nEXCERPTS:\n'+'\n\n'.join(f"SOURCE {z['path']} @{z['offset']}: {z['text']}" for z in ps)+'\n\nPREVIOUS SCORED WORK (different questions):\n'+json.dumps(selected,ensure_ascii=False,separators=(',',':'))
   if len(content)>8000:raise RuntimeError('input too long')
   item={'task_key':'local_research/'+key,'question':q,'official_expected':t['answer'],'history_before':len(records),'history_used':selected,'retrieval_refs':refs,'input_sha256':prior.sha(content),'runs':[]};result['tasks'].append(item);flush()
   for arm in ('baseline','candidate'):
    if 'task_id' not in tasks[arm]:
     reg=post('/api/work-designs/'+tasks[arm]['design']+'/register',{'revision':2,'source_text':content,'model_mode':'LIVE_LLM','schedule':''});task_id=reg['task_id'];tasks[arm]['task_id']=task_id;versions[arm]=1
    else:
     task_id=tasks[arm]['task_id'];r=client.put(f'/api/tasks/{task_id}/text-source',json={'source_text':content,'expected_version':versions[arm],'idempotency_key':f'candidate-source-{i}-{arm}'})
     if r.status_code!=200:raise RuntimeError(r.text)
     versions[arm]=r.json()['version']
    t0=time.monotonic();tr=post(f'/api/tasks/{task_id}/run',{'idempotency_key':f'candidate-run-{i}-{arm}','input_snapshot':{}});claim=kernel.claim()
    if not claim or str(claim['work_id'])!=tr['work_id']:raise RuntimeError('unexpected claim')
    outcome=process_claim(kernel,claim);elapsed=round((time.monotonic()-t0)*1000);w=client.get(f"/api/work/{tr['work_id']}").json();info=client.get(f"/api/runs/{claim['run_id']}").json();arts=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()
    rec={'arm':arm,'task_id':task_id,'work_id':tr['work_id'],'run_id':str(claim['run_id']),'wall_ms':elapsed,'work_status':w['work']['status'],'run_status':info['status'],'run_error':info['error'],'outcome':outcome}
    if arts:
     a=arts[0];s=a['content']['steps'][0];prediction=s['text'].strip();man=info['context']['manifest'];rec.update(prediction=prediction,score=mod.score(t,prediction),artifact_id=a['artifact_id'],artifact_sha256=a['sha256'],artifact_content=a['content'],evidence=a['evidence'],context_sha256=info['context']['sha256'],source_sha256=man['source_sha256'],contract_sha256=man['contract_sha256'],input_sha256=prior.sha(content),model=s['model_id'],response_id=s['response_id'],input_tokens=s['input_tokens'],output_tokens=s['output_tokens'])
    else:rec.update(prediction=None,score=0)
    item['runs'].append(rec);flush();print(key,arm,'score',rec['score'],'status',rec['work_status'],flush=True)
   record=item['runs'][0]
   if record.get('artifact_id'):
    with engine.begin() as c:
     a=c.execute(text('SELECT content,sha256 FROM artifacts WHERE artifact_id=:id'),{'id':record['artifact_id']}).mappings().one()
     if digest(a['content'])!=a['sha256'] or a['sha256']!=record['artifact_sha256']:raise RuntimeError('artifact changed')
     c.execute(text('''INSERT INTO hcast_feedback_observations(seq,task_key,question,prediction,expected,score,work_id,run_id,artifact_id,artifact_sha256,scorer_version) VALUES (:seq,:task,:q,:p,CAST(:expected AS jsonb),:score,:work,:run,:artifact,:sha,:ver)'''),{'seq':200+i,'task':item['task_key'],'q':q,'p':record['prediction'],'expected':json.dumps(t['answer'],ensure_ascii=False),'score':int(record['score']),'work':record['work_id'],'run':record['run_id'],'artifact':record['artifact_id'],'sha':record['artifact_sha256'],'ver':prior.REV})
   item['history_after']=len(history());flush()
 result['task_ids']={k:x['task_id'] for k,x in tasks.items()};flush();print('saved',OUT,flush=True)
if __name__=='__main__':main()
