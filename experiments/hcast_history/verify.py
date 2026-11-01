"""Verify recorded HCAST outputs, official scoring, source and history boundaries."""
import json, os, subprocess, sys
from pathlib import Path
import run as prior
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from app.domain.hashing import digest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
ROOT=Path(__file__).resolve().parents[2]; E=ROOT/'docs/evidence/hcast_history'
root=Path(os.environ['HCAST_ROOT']);tex=Path(os.environ['HCAST_TEX_ASSETS'])
assert subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()==prior.REV
_,chunks=prior.corpus(root,tex); lookup={(x['path'],x['offset']):x for x in chunks}
mods={f:prior.module(root/f/(f+'.py')).TaskFamily for f in ('local_research','local_research_tex')}
source=json.loads((E/'run.json').read_text(encoding='utf-8'));follow=json.loads((E/'followup.json').read_text(encoding='utf-8'));candidate=json.loads((E/'candidate.json').read_text(encoding='utf-8'));recovery=json.loads((E/'recovery.json').read_text(encoding='utf-8'))
v=TfidfVectorizer(stop_words='english',ngram_range=(1,2),sublinear_tf=True);matrix=v.fit_transform([c['text'] for c in chunks])
known={};counts={'runs':0,'success':0,'failed':0}

def matches(refs,q):
 scores=cosine_similarity(v.transform([q]),matrix).ravel();picks=sorted(range(len(chunks)),key=lambda j:(-scores[j],j))[:4]
 assert [(chunks[j]['path'],chunks[j]['offset'],chunks[j]['sha256']) for j in picks]==[(r['path'],r['offset'],r['sha256']) for r in refs]
 return [lookup[(r['path'],r['offset'])] for r in refs]
def inputbase(item):
 ps=matches(item['retrieval_refs'],item['question'])
 return 'QUESTION: '+item['question']+'\n\nEXCERPTS:\n'+'\n\n'.join(f"SOURCE {z['path']} @{z['offset']}: {z['text']}" for z in ps)
def validate_history(records):
 for h in records:
  assert h['artifact_id'] in known
  prior_row=known[h['artifact_id']]
  for key in ('task_key','question','artifact_sha256'):
   assert h[key]==prior_row[key]
  assert h.get('prediction',h.get('answer'))==prior_row['prediction']
  if 'answer' in h:assert prior_row['score']==1
  if 'expected' in h:assert h['expected']==prior_row['expected'] and h['score']==prior_row['score']
def verify_row(item,row,content):
 family,key=item['task_key'].split('/');task=mods[family].get_tasks()[key];assert item['question']==task['prompt'].strip() and item['official_expected']==task['answer']
 counts['runs']+=1
 if row['run_status']=='FAILED':
  assert row['score']==0 and row['prediction'] is None;counts['failed']+=1;return
 counts['success']+=1
 assert row['run_status']=='SUCCEEDED' and row['work_status']=='WAITING_APPROVAL'
 a=row['artifact_content'];assert digest(a)==row['artifact_sha256'] and a['semantic_review']=='NOT_PERFORMED' and a['model_mode']=='LIVE_LLM'
 assert a['steps'][0]['text'].strip()==row['prediction'] and row['score']==mods[family].score(task,row['prediction'])
 assert a['source_sha256']==row['source_sha256']==digest(content) and row['input_sha256']==prior.sha(content)
 assert row['evidence'][0]['result']['passed'] and row['evidence'][0]['result']['requires_human_review']
def remember(item):
 row=item['runs'][0]
 if row.get('artifact_id'):known[row['artifact_id']]={'task_key':item['task_key'],'question':item['question'],'prediction':row['prediction'],'expected':item['official_expected'],'score':row['score'],'artifact_sha256':row['artifact_sha256']}

for item in source['tasks']:
 b=inputbase(item);sel=item['selected_history'];validate_history(sel)
 for row in item['runs']:
  content=b
  if row['arm']=='history':content+='\n\nPREVIOUS VERIFIED WORK (not the current answer):\n'+(json.dumps(sel,ensure_ascii=False) if sel else '(none)')
  verify_row(item,row,content)
 remember(item)
for item in follow['tasks']:
 b=inputbase(item);sel=item['history_used'];validate_history(sel)
 for row in item['runs']:
  content=b
  if row['arm']=='history':content+='\n\nSCORED PREVIOUS WORK; avoid repeating prior errors and follow the demonstrated exact-answer format. These examples are different questions and do not answer this question:\n'+json.dumps(sel,ensure_ascii=False,separators=(',',':'))
  verify_row(item,row,content)
 remember(item)
assert all(candidate['baseline_plan'][k]==candidate['candidate_plan'][k] for k in candidate['baseline_plan'] if k!='steps')
a,b=candidate['baseline_plan']['steps'][0],candidate['candidate_plan']['steps'][0]
assert all(a[k]==b[k] for k in a if k!='purpose') and b['purpose'].startswith(a['purpose'])
for item in candidate['tasks']:
 sel=item['history_used'];validate_history(sel);content=inputbase(item)+'\n\nPREVIOUS SCORED WORK (different questions):\n'+json.dumps(sel,ensure_ascii=False,separators=(',',':'))
 for row in item['runs']:verify_row(item,row,content)
 remember(item)
item=next(t for t in source['tasks'] if t['task_key']==recovery['task_key']);validate_history(recovery['history_used'])
content=inputbase(item)+'\n\nPREVIOUS SCORED WORK (different questions):\n'+json.dumps(recovery['history_used'],ensure_ascii=False,separators=(',',':'))
verify_row(item,recovery,content)
print('Verified official scorer, previous-only history, retrieved excerpts, source and artifact hashes, and paired inputs:',counts)
print('History-only paired results:',sum(t['runs'][0]['score'] for t in follow['tasks']),sum(t['runs'][1]['score'] for t in follow['tasks']))
print('Candidate paired results:',sum(t['runs'][0]['score'] for t in candidate['tasks']),sum(t['runs'][1]['score'] for t in candidate['tasks']))
