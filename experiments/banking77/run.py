"""Public BANKING77 paired benchmark through the existing Work OS runtime.

Run only on a disposable PostgreSQL DB named banking77_poc. Requires separately
configured credentials; never writes credentials. Snapshot and results are public.
"""
import csv, hashlib, io, json, os, platform, re, sys, time, urllib.request
from collections import Counter
from pathlib import Path

REV='57ec275d8078af65b7731c2a98be812d844a6d6b'
BASE=f'https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/{REV}/banking_data/'
EXPECTED={'train.csv':'b06e26ac675513959a63135f11b94ea7786ed02da65db93a5650d8838cbc664b','test.csv':'d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d'}
CATEGORIES=['card_arrival','card_delivery_estimate','card_linking','card_payment_fee_charged','card_payment_not_recognised','card_payment_wrong_exchange_rate','card_swallowed','declined_card_payment']
SEED='workos-banking77-holdout-v1'
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/evidence/banking77'

def sha(b): return hashlib.sha256(b).hexdigest()
def getdata(name):
    raw=urllib.request.urlopen(BASE+name,timeout=60).read()
    if sha(raw)!=EXPECTED[name]:raise RuntimeError('upstream hash mismatch: '+name)
    return list(csv.DictReader(io.StringIO(raw.decode('utf-8'))))
def canonical(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def sample(rows):
    chosen=[]
    for category in CATEGORIES:
        selected=[(i+2,r) for i,r in enumerate(rows) if r['category']==category]
        selected.sort(key=lambda pair:sha(f'{SEED}:{pair[0]}'.encode()))
        chosen.extend({'source_csv_record_number':line,'text':r['text'],'expected':r['category']} for line,r in selected[:5])
    chosen.sort(key=lambda x:sha(f"{SEED}:order:{x['source_csv_record_number']}".encode()))
    for i,x in enumerate(chosen): x['id']=f'Q{i+1:02}'
    return chosen

def main():
    if not os.environ.get('DATABASE_URL','').endswith('/banking77_poc'):
        raise RuntimeError('Set DATABASE_URL to an isolated database named banking77_poc')
    if not (os.environ.get('openai_api_key') or os.environ.get('OPENAI_API_KEY')):raise RuntimeError('API key absent')
    train=getdata('train.csv'); test=getdata('test.csv'); rows=sample(test)
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.svm import LinearSVC
    import sklearn
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.work.db import engine
    from app.work.service import Kernel
    from app.worker import process_claim
    from sqlalchemy import text
    training=[r for r in train if r['category'] in CATEGORIES]
    vec=TfidfVectorizer(ngram_range=(1,2),sublinear_tf=True,strip_accents='unicode')
    start=time.monotonic(); X=vec.fit_transform([r['text'] for r in training]); clf=LinearSVC(random_state=0);clf.fit(X,[r['category'] for r in training]); train_ms=round((time.monotonic()-start)*1000)
    start=time.monotonic(); predictions=clf.predict(vec.transform([r['text'] for r in rows])).tolist();baseline_ms=round((time.monotonic()-start)*1000)
    for r,p in zip(rows,predictions):r['baseline_prediction']=p
    OUT.mkdir(parents=True,exist_ok=True)
    doc={'schema':'banking77-workos-evidence-v1','source_revision':REV,'source_urls':{x:BASE+x for x in EXPECTED},'source_sha256':EXPECTED,'sampling':{'seed':SEED,'per_category':5,'category_order':CATEGORIES,'selection':'sha256(seed:csv_record_number), ascending; ordering sha256(seed:order:csv_record_number), ascending','holdout_split':'official test.csv','train_split':'official train.csv','train_rows':len(training)},'baseline':{'name':'TF-IDF word 1-2gram + LinearSVC','sklearn_version':sklearn.__version__,'training_ms':train_ms,'inference_ms':baseline_ms},'environment':{'python':platform.python_version(),'model_configured':os.environ.get('openai_model') or os.environ.get('OPENAI_MODEL')},'cases':rows,'runs':[]}
    dest=OUT/'run.json'
    def flush():dest.write_text(json.dumps(doc,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    flush()
    codes={f'T{i+1}':v for i,v in enumerate(CATEGORIES)}
    definitions='; '.join(f'{k}={v}' for k,v in codes.items())
    purpose=('Classify each query in source_text into one of these eight banking intent codes: '+definitions+'. '
             'Return only one line per query in exact format Q01|T1. Preserve each query ID, output one code per query. No explanations.')
    plan={'name':'BANKING77 intent routing evaluation','objective':'Classify each provided banking query into exactly one intent code.',
          'expected_result':'One ID and one intent code per input query.', 'evaluation_criteria':['Every input has exactly one allowed code.'],
          'steps':[{'id':'classify','title':'Classify customer queries','purpose':purpose,'depends_on':[]}], 'open_questions':[]}
    batches=[rows[i:i+10] for i in range(0,len(rows),10)]
    inputs=['\n'.join(f"{x['id']} {x['text']}" for x in batch) for batch in batches]
    with TestClient(app) as client:
        def post(path,data):
            res=client.post(path,json=data)
            if res.status_code!=200:raise RuntimeError(f'{path}: HTTP {res.status_code} {res.text[:500]}')
            return res.json()
        d=post('/api/work-designs',{'description':'Classify published BANKING77 query intents','mode':'MOCK'})
        res=client.put('/api/work-designs/'+d['design_id'],json={'revision':1,'plan':plan})
        if res.status_code!=200:raise RuntimeError(res.text)
        reg=post('/api/work-designs/'+d['design_id']+'/register',{'revision':2,'source_text':inputs[0],'model_mode':'LIVE_LLM','schedule':''})
        task=reg['task_id'];doc['task_id']=task;doc['design_id']=d['design_id'];doc['plan']=plan;doc['codes']=codes;flush()
        kernel=Kernel(engine)
        for index,(batch,source) in enumerate(zip(batches,inputs)):
            if index:
                res=client.put(f'/api/tasks/{task}/text-source',json={'source_text':source,'expected_version':index,'idempotency_key':f'banking77-source-{index}'})
                if res.status_code!=200:raise RuntimeError('source: '+res.text)
            t=time.monotonic(); started=post(f'/api/tasks/{task}/run',{'idempotency_key':f'banking77-batch-{index}','input_snapshot':{}})
            claim=kernel.claim()
            if not claim or str(claim['work_id'])!=started['work_id']:raise RuntimeError('unexpected queue claim')
            outcome=process_claim(kernel,claim);wall=round((time.monotonic()-t)*1000)
            detail=client.get(f"/api/work/{started['work_id']}").json(); info=client.get(f"/api/runs/{claim['run_id']}").json()
            arts=client.get(f"/api/runs/{claim['run_id']}/artifacts").json()
            run={'batch':index+1,'case_ids':[x['id'] for x in batch],'work_id':started['work_id'],'run_id':str(claim['run_id']),'wall_ms':wall,'outcome':outcome,'work_status':detail['work']['status'],'run_info':info,'artifacts':arts}
            doc['runs'].append(run);flush()
            print('batch',index+1,'status',run['work_status'],'artifacts',len(arts),flush=True)
            if arts:
                output=arts[0]['content']['steps'][0]['text']
                matches=re.findall(r'(?im)^\s*(Q\d{2})\s*\|\s*(T[1-8])\s*$',output)
                counts=Counter(q for q,_ in matches);lookup=dict(matches)
                for case in batch:
                    case['workos_prediction']=codes.get(lookup.get(case['id'])) if counts[case['id']]==1 else None
            else:
                for case in batch:case['workos_prediction']=None
            flush()
    print('baseline',sum(x['expected']==x['baseline_prediction'] for x in rows),'workos',sum(x['expected']==x.get('workos_prediction') for x in rows),'n',len(rows),flush=True)

if __name__=='__main__':main()
