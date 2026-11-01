"""Evaluate historical Work OS records on public METR HCAST tasks.

Uses an isolated DB ending /hcast_history_poc. HCAST_ROOT and HCAST_TEX_ASSETS
point to the checked-out public suite and its DVC text assets respectively.
Outputs contain no corpus passages: source hashes and offsets permit inspection.
"""
import hashlib, importlib.util, json, os, re, time
from collections import Counter
from pathlib import Path
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from pypdf import PdfReader
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/evidence/hcast_history/run.json'
REV='03438239d200fb3e89a5ddfc62ab4d23cc1866fb'
QUESTIONS=[('local_research','atari_epochs'),('local_research','which_does_worse'),('local_research_tex','33b_better_than_65b'),('local_research_tex','more_bias'),('local_research_tex','ar_report')]

def sha(s):return hashlib.sha256(s.encode('utf-8')).hexdigest()
def module(path):
    spec=importlib.util.spec_from_file_location(path.stem,path); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def corpus(root,tex):
    docs=[]
    for p in sorted(tex.rglob('*')):
        if p.is_file() and p.suffix in ('.tex','.bbl'):
            docs.append(('tex/'+p.relative_to(tex).as_posix(),p.read_text(encoding='utf-8',errors='replace')))
    for p in sorted((root/'local_research/assets').glob('*')):
        if p.suffix=='.pdf':
            text='\n'.join(page.extract_text() or '' for page in PdfReader(str(p)).pages)
        elif p.suffix=='.html':
            text=BeautifulSoup(p.read_text(encoding='utf-8',errors='replace'),'html.parser').get_text(' ',strip=True)
        else:continue
        docs.append(('research/'+p.name,text))
    chunks=[]
    for name,text in docs:
        text=re.sub(r'\s+',' ',text).strip()
        for offset in range(0,len(text),800):
            chunk=text[offset:offset+1200]
            if len(chunk)>80:chunks.append({'path':name,'offset':offset,'text':chunk,'sha256':sha(chunk)})
    return docs,chunks

def main():
    if not os.environ.get('DATABASE_URL','').endswith('/hcast_history_poc'):raise RuntimeError('Use only the isolated hcast_history_poc database')
    if not (os.environ.get('openai_api_key') or os.environ.get('OPENAI_API_KEY')):raise RuntimeError('LLM key absent')
    root=Path(os.environ['HCAST_ROOT']);tex=Path(os.environ['HCAST_TEX_ASSETS'])
    docs,chunks=corpus(root,tex)
    vectorizer=TfidfVectorizer(stop_words='english',ngram_range=(1,2),sublinear_tf=True)
    matrix=vectorizer.fit_transform([c['text'] for c in chunks])
    mods={fam:module(root/fam/(fam+'.py')).TaskFamily for fam in ('local_research','local_research_tex')}
    tasks=[{'family':fam,'task':key,**mods[fam].get_tasks()[key]} for fam,key in QUESTIONS]
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from app.api.main import app
    from app.work.db import engine
    from app.work.service import Kernel
    from app.worker import process_claim
    from app.domain.hashing import digest
    plan={'name':'HCAST public local research task','objective':'Answer the research question using the supplied excerpts; use previous verified records only as supplementary context.',
      'expected_result':'A single short answer in English, no explanation.', 'evaluation_criteria':['A short answer is returned for independent evaluation.'],
      'steps':[{'id':'answer','title':'Find the requested fact','purpose':'Read the question and excerpts. Give only the short answer; no explanation, answer prefix or extra text. Prior completed task records may be relevant but do not assume they answer the current question.','depends_on':[]}], 'open_questions':[]}
    result={'schema':'hcast-history-poc-v1','source_revision':REV,'human_time_reference':{'task':'local_research_tex/ar_report','minutes':24,'kind':'METR published baseline time','source':'https://openreview.net/pdf?id=CGNJL6CeV0','comparable':False},
       'dataset':['https://github.com/METR/hcast-public/tree/'+REV+'/local_research','https://github.com/METR/hcast-public/tree/'+REV+'/local_research_tex'],
       'corpus':{'retrieval':'TF-IDF, word 1-2 grams, english stopwords; 1200 characters, stride 800, top 4; ties by source index','documents':[{'path':p,'text_sha256':sha(v),'characters':len(v)} for p,v in docs],'chunks':len(chunks)},
       'history_policy':'Earlier independently scored successful baseline Work artifacts only, ranked by TF-IDF question similarity with threshold 0.08; maximum 2. Current/future records excluded. No human approval claimed.',
       'plan':plan,'tasks':[]}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    def flush():OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    def select_history(question,history):
        if not history:return []
        v=TfidfVectorizer(stop_words='english',ngram_range=(1,2));X=v.fit_transform([h['question'] for h in history]+[question]); sims=cosine_similarity(X[-1],X[:-1]).ravel()
        return [dict(history[i],similarity=round(float(sims[i]),8)) for i in sorted(range(len(sims)),key=lambda i:(-sims[i],i)) if sims[i]>=0.08][:2]
    with TestClient(app) as client:
        def post(path,data):
            r=client.post(path,json=data)
            if r.status_code!=200:raise RuntimeError(path+' '+str(r.status_code)+' '+r.text[:300])
            return r.json()
        with engine.begin() as c:
            c.execute(text('''CREATE TABLE IF NOT EXISTS hcast_history_observations (
              seq integer primary key, task_key text not null, question text not null, answer text not null,
              work_id uuid not null, run_id uuid not null, artifact_id uuid not null, artifact_sha256 text not null,
              score integer not null check (score=1), scorer_version text not null, created_at timestamptz not null default now())'''))
            c.execute(text("""CREATE OR REPLACE FUNCTION prevent_hcast_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'history observations are append-only'; END $$"""))
            c.execute(text('DROP TRIGGER IF EXISTS hcast_history_immutable ON hcast_history_observations'))
            c.execute(text('CREATE TRIGGER hcast_history_immutable BEFORE UPDATE OR DELETE ON hcast_history_observations FOR EACH ROW EXECUTE FUNCTION prevent_hcast_history_mutation()'))
        def history_rows():
            with engine.connect() as c:
                rows=c.execute(text('''SELECT h.seq,h.task_key,h.question,h.answer,h.work_id::text,h.run_id::text,h.artifact_id::text,h.artifact_sha256,a.content,a.sha256
                    FROM hcast_history_observations h JOIN artifacts a ON a.artifact_id=h.artifact_id
                    JOIN runs r ON r.run_id=h.run_id AND r.status='SUCCEEDED' WHERE h.score=1 ORDER BY h.seq''')).mappings().all()
            for h in rows:
                if h['sha256']!=h['artifact_sha256'] or digest(h['content'])!=h['sha256'] or h['content']['steps'][0]['text']!=h['answer']:raise RuntimeError('history artifact failed integrity check')
            return [dict(seq=h['seq'],task_key=h['task_key'],question=h['question'],answer=h['answer'],work_id=h['work_id'],run_id=h['run_id'],artifact_id=h['artifact_id'],artifact_sha256=h['artifact_sha256']) for h in rows]
        draft=post('/api/work-designs',{'description':'Investigate a question using local research sources','mode':'MOCK'})
        edited=client.put('/api/work-designs/'+draft['design_id'],json={'revision':1,'plan':plan})
        if edited.status_code!=200:raise RuntimeError(edited.text)
        task_id=None;source_version=0; kernel=Kernel(engine)
        for i,t in enumerate(tasks):
            q=t['prompt'].strip();query=vectorizer.transform([q]);scores=cosine_similarity(query,matrix).ravel()
            picks=sorted(range(len(chunks)),key=lambda j:(-scores[j],j))[:4]
            passages=[chunks[j] for j in picks]
            refs=[{'path':v['path'],'offset':v['offset'],'sha256':v['sha256'],'similarity':round(float(scores[j]),8)} for j,v in zip(picks,passages)]
            past=history_rows();selected=select_history(q,past)
            item={'index':i+1,'task_key':t['family']+'/'+t['task'],'question':q,'official_expected':t['answer'],'history_before':len(past),'selected_history':selected,'retrieval_refs':refs,'runs':[]}
            result['tasks'].append(item);flush()
            for arm in (['baseline'] if i==0 else ['baseline','history']):
                content='QUESTION: '+q+'\n\nEXCERPTS:\n'+'\n\n'.join(f"SOURCE {z['path']} @{z['offset']}: {z['text']}" for z in passages)
                if arm=='history':content+='\n\nPREVIOUS VERIFIED WORK (not the current answer):\n'+(json.dumps(selected,ensure_ascii=False) if selected else '(none)')
                if len(content)>8000:raise RuntimeError('input exceeds workos text limit')
                if task_id is None:
                    reg=post('/api/work-designs/'+draft['design_id']+'/register',{'revision':2,'source_text':content,'model_mode':'LIVE_LLM','schedule':''});task_id=reg['task_id'];source_version=1;result['task_id']=task_id;result['design_id']=draft['design_id']
                else:
                    r=client.put(f'/api/tasks/{task_id}/text-source',json={'source_text':content,'expected_version':source_version,'idempotency_key':f'hcast-source-{i}-{arm}'})
                    if r.status_code!=200:raise RuntimeError('source update '+r.text)
                    source_version=r.json()['version']
                start=time.monotonic();trigger=post(f'/api/tasks/{task_id}/run',{'idempotency_key':f'hcast-run-{i}-{arm}','input_snapshot':{}})
                claim=kernel.claim()
                if not claim or str(claim['work_id'])!=trigger['work_id']:raise RuntimeError('unexpected claim')
                outcome=process_claim(kernel,claim);wall=round((time.monotonic()-start)*1000)
                work=client.get(f"/api/work/{trigger['work_id']}").json();info=client.get(f"/api/runs/{claim['run_id']}").json();art=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()
                record={'arm':arm,'work_id':trigger['work_id'],'run_id':str(claim['run_id']),'source_version':source_version,'wall_ms':wall,'work_status':work['work']['status'],'run_status':info['status'],'run_error':info['error'],'outcome':outcome,'artifacts':[]}
                if art:
                    a=art[0];step=a['content']['steps'][0];prediction=step['text'].strip();score=mods[t['family']].score(t,prediction)
                    manifest=info['context']['manifest'];record.update(prediction=prediction,score=score,artifact_id=a['artifact_id'],artifact_sha256=a['sha256'],artifact_content=a['content'],evidence=a['evidence'],context_sha256=info['context']['sha256'],source_sha256=manifest['source_sha256'],contract_sha256=manifest['contract_sha256'],input_sha256=sha(content),retrieved_refs=refs,model=step['model_id'],response_id=step['response_id'],input_tokens=step['input_tokens'],output_tokens=step['output_tokens'],model_elapsed_ms=step['elapsed_ms'])
                else:record.update(prediction=None,score=0)
                item['runs'].append(record);flush();print(item['task_key'],arm,'score',record['score'],'status',record['work_status'],'history',len(selected) if arm=='history' else 0,flush=True)
            first=item['runs'][0]
            if first.get('score')==1:
                with engine.begin() as c:
                    c.execute(text('''INSERT INTO hcast_history_observations(seq,task_key,question,answer,work_id,run_id,artifact_id,artifact_sha256,score,scorer_version)
                       VALUES (:seq,:task,:question,:answer,:work,:run,:artifact,:sha,1,:scorer)'''),{'seq':i+1,'task':item['task_key'],'question':q,'answer':first['prediction'],'work':first['work_id'],'run':first['run_id'],'artifact':first['artifact_id'],'sha':first['artifact_sha256'],'scorer':REV})
            item['history_after']=len(history_rows());flush()
    print('saved',OUT,flush=True)
if __name__=='__main__':main()
