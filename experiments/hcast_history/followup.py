"""Prequential follow-up: paired held-out tasks with accumulating scored prior records."""
import json, os, time
from pathlib import Path
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import run as prior

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/evidence/hcast_history/followup.json'
TEST=['author_letter','sentence_letter_theodore','td_year_adam','atari_parameters_amy','task_not_moving_amy']

def main():
 if not os.getenv('DATABASE_URL','').endswith('/hcast_history_poc'):raise RuntimeError('Requires the isolated hcast_history_poc DB from run.py')
 root=Path(os.environ['HCAST_ROOT']);tex=Path(os.environ['HCAST_TEX_ASSETS']);docs,chunks=prior.corpus(root,tex)
 source=json.loads((ROOT/'docs/evidence/hcast_history/run.json').read_text(encoding='utf-8'))
 mod=prior.module(root/'local_research/local_research.py').TaskFamily;tm=prior.module(root/'local_research_tex/local_research_tex.py').TaskFamily
 mods={'local_research':mod,'local_research_tex':tm}
 v=TfidfVectorizer(stop_words='english',ngram_range=(1,2),sublinear_tf=True);matrix=v.fit_transform([x['text'] for x in chunks])
 from fastapi.testclient import TestClient
 from sqlalchemy import text
 from app.api.main import app
 from app.work.db import engine
 from app.work.service import Kernel
 from app.worker import process_claim
 from app.domain.hashing import digest
 plan=source['plan'];result={'schema':'hcast-prequential-feedback-v1','source_revision':prior.REV,'development_record_file':'run.json','design':'Five previously unexecuted HCAST task variants. Each pair shares identical retrieved context and method; treatment adds up to four previous scored Work outputs with official answers. Prior baseline result is appended only after both arms finish. No current/future answer enters either input.', 'test_order':TEST,'tasks':[]}
 def flush():OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
 with TestClient(app) as client:
  with engine.begin() as c:
   c.execute(text('''CREATE TABLE IF NOT EXISTS hcast_feedback_observations(seq integer primary key,task_key text not null,question text not null,prediction text not null,expected jsonb not null,score integer not null, work_id uuid not null,run_id uuid not null,artifact_id uuid not null,artifact_sha256 text not null,scorer_version text not null,created_at timestamptz not null default now())'''))
   c.execute(text("""CREATE OR REPLACE FUNCTION prevent_hcast_feedback_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'feedback is append-only'; END $$"""))
   c.execute(text('DROP TRIGGER IF EXISTS hcast_feedback_immutable ON hcast_feedback_observations'))
   c.execute(text('CREATE TRIGGER hcast_feedback_immutable BEFORE UPDATE OR DELETE ON hcast_feedback_observations FOR EACH ROW EXECUTE FUNCTION prevent_hcast_feedback_mutation()'))
   if c.execute(text('SELECT count(*) FROM hcast_feedback_observations')).scalar_one():raise RuntimeError('feedback table already populated; refuse overwrite')
  def add(seq,key,q,ans,expected,score,r):
   with engine.begin() as c:
    a=c.execute(text('SELECT content,sha256 FROM artifacts WHERE artifact_id=:id'),{'id':r['artifact_id']}).mappings().one()
    if a['sha256']!=r['artifact_sha256'] or digest(a['content'])!=a['sha256'] or a['content']['steps'][0]['text'].strip()!=ans:raise RuntimeError('artifact integrity failed')
    c.execute(text('''INSERT INTO hcast_feedback_observations(seq,task_key,question,prediction,expected,score,work_id,run_id,artifact_id,artifact_sha256,scorer_version)
     VALUES (:seq,:task,:q,:p,CAST(:expected AS jsonb),:score,:work,:run,:artifact,:sha,:ver)'''),{'seq':seq,'task':key,'q':q,'p':ans,'expected':json.dumps(expected,ensure_ascii=False),'score':int(score),'work':r['work_id'],'run':r['run_id'],'artifact':r['artifact_id'],'sha':r['artifact_sha256'],'ver':prior.REV})
  def history():
   with engine.connect() as c:
    rows=c.execute(text('''SELECT h.seq,h.task_key,h.question,h.prediction,h.expected,h.score,h.work_id::text,h.run_id::text,h.artifact_id::text,h.artifact_sha256,a.sha256,a.content
      FROM hcast_feedback_observations h JOIN artifacts a ON a.artifact_id=h.artifact_id JOIN runs r ON r.run_id=h.run_id AND r.status='SUCCEEDED' ORDER BY h.seq''')).mappings().all()
   for h in rows:
    if h['sha256']!=h['artifact_sha256'] or digest(h['content'])!=h['sha256']:raise RuntimeError('history changed')
   return [{k:h[k] for k in ('seq','task_key','question','prediction','expected','score','work_id','run_id','artifact_id','artifact_sha256')} for h in rows]
  # Import only real earlier baseline artifacts; missing/failed execution remains in run.json, not invented as an answer.
  for item in source['tasks']:
   r=item['runs'][0]
   if not r.get('artifact_id'):continue
   family,key=item['task_key'].split('/');t=mods[family].get_tasks()[key];score=mods[family].score(t,r['prediction'])
   if score!=r['score']:raise RuntimeError('historical score mismatch')
   add(item['index'],item['task_key'],item['question'],r['prediction'],item['official_expected'],score,r)
  def post(path,data):
   r=client.post(path,json=data)
   if r.status_code!=200:raise RuntimeError(path+' '+r.text[:300])
   return r.json()
  draft=post('/api/work-designs',{'description':'Continue local research with measured task feedback','mode':'MOCK'})
  r=client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan})
  if r.status_code!=200:raise RuntimeError(r.text)
  kernel=Kernel(engine);task_id=None;source_version=0
  for i,key in enumerate(TEST):
   t=mod.get_tasks()[key];q=t['prompt'].strip();scores=cosine_similarity(v.transform([q]),matrix).ravel();picks=sorted(range(len(chunks)),key=lambda j:(-scores[j],j))[:4];passages=[chunks[j] for j in picks]
   refs=[{'path':z['path'],'offset':z['offset'],'sha256':z['sha256']} for z in passages];past=history();selected=past[-4:]
   item={'index':i+1,'task_key':'local_research/'+key,'question':q,'official_expected':t['answer'],'history_before':len(past),'history_used':selected,'retrieval_refs':refs,'runs':[]};result['tasks'].append(item);flush()
   for arm in ('baseline','history'):
    content='QUESTION: '+q+'\n\nEXCERPTS:\n'+'\n\n'.join(f"SOURCE {z['path']} @{z['offset']}: {z['text']}" for z in passages)
    if arm=='history':content+='\n\nSCORED PREVIOUS WORK; avoid repeating prior errors and follow the demonstrated exact-answer format. These examples are different questions and do not answer this question:\n'+json.dumps(selected,ensure_ascii=False,separators=(',',':'))
    if len(content)>8000:raise RuntimeError('input too long')
    if task_id is None:
     reg=post('/api/work-designs/'+draft['design_id']+'/register',{'revision':2,'source_text':content,'model_mode':'LIVE_LLM','schedule':''});task_id=reg['task_id'];source_version=1;result['task_id']=task_id
    else:
     r=client.put(f'/api/tasks/{task_id}/text-source',json={'source_text':content,'expected_version':source_version,'idempotency_key':f'feedback-source-{i}-{arm}'})
     if r.status_code!=200:raise RuntimeError(r.text)
     source_version=r.json()['version']
    start=time.monotonic();tr=post(f'/api/tasks/{task_id}/run',{'idempotency_key':f'feedback-run-{i}-{arm}','input_snapshot':{}});claim=kernel.claim()
    if not claim or str(claim['work_id'])!=tr['work_id']:raise RuntimeError('unexpected claim')
    outcome=process_claim(kernel,claim);elapsed=round((time.monotonic()-start)*1000);w=client.get(f"/api/work/{tr['work_id']}").json();inf=client.get(f"/api/runs/{claim['run_id']}").json();arts=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()
    row={'arm':arm,'work_id':tr['work_id'],'run_id':str(claim['run_id']),'source_version':source_version,'wall_ms':elapsed,'work_status':w['work']['status'],'run_status':inf['status'],'run_error':inf['error'],'outcome':outcome}
    if arts:
     a=arts[0];s=a['content']['steps'][0];prediction=s['text'].strip();manifest=inf['context']['manifest']
     row.update(prediction=prediction,score=mod.score(t,prediction),artifact_id=a['artifact_id'],artifact_sha256=a['sha256'],artifact_content=a['content'],evidence=a['evidence'],context_sha256=inf['context']['sha256'],source_sha256=manifest['source_sha256'],contract_sha256=manifest['contract_sha256'],input_sha256=prior.sha(content),model=s['model_id'],response_id=s['response_id'],input_tokens=s['input_tokens'],output_tokens=s['output_tokens'])
    else:row.update(prediction=None,score=0)
    item['runs'].append(row);flush();print(item['task_key'],arm,'score',row['score'],'status',row['work_status'],'records',len(selected) if arm=='history' else 0,flush=True)
   r=item['runs'][0]
   if r.get('artifact_id'):add(100+i,item['task_key'],q,r['prediction'],t['answer'],r['score'],r)
   item['history_after']=len(history());flush()
 print('saved',OUT,flush=True)
if __name__=='__main__':main()
