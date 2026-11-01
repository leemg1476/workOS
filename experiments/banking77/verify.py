"""Independently recompute source sampling, baseline and recorded output scores."""
import json, re, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from app.domain.hashing import digest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.svm import LinearSVC
import run

x=json.loads((run.OUT/'run.json').read_text(encoding='utf-8'))
assert x['source_revision']==run.REV and x['source_sha256']==run.EXPECTED
train=run.getdata('train.csv');test=run.getdata('test.csv');selection=run.sample(test)
assert all(all(a[k]==b[k] for k in ('id','text','expected','source_csv_record_number')) for a,b in zip(selection,x['cases']))
assert len(selection)==len(x['cases'])==40
training=[r for r in train if r['category'] in run.CATEGORIES]
vec=TfidfVectorizer(ngram_range=(1,2),sublinear_tf=True,strip_accents='unicode')
clf=LinearSVC(random_state=0);clf.fit(vec.fit_transform([r['text'] for r in training]),[r['category'] for r in training])
pred=clf.predict(vec.transform([r['text'] for r in selection])).tolist()
assert pred==[c['baseline_prediction'] for c in x['cases']]
cases={c['id']:c for c in x['cases']};codes=x['codes'];seen=[]
for item in x['runs']:
    info=item['run_info'];artifact=item['artifacts'][0]; content=artifact['content'];manifest=info['context']['manifest'];ev=artifact['evidence'][0]
    assert item['work_status']=='WAITING_APPROVAL' and item['outcome']['passed'] and ev['result']['passed']
    assert info['status']=='SUCCEEDED' and content['model_mode']=='LIVE_LLM' and content['semantic_review']=='NOT_PERFORMED'
    assert digest(content)==artifact['sha256'] and digest(manifest)==info['context']['sha256']
    assert digest(manifest['contract']['source_text'])==manifest['source_sha256']==content['source_sha256']
    assert manifest['contract']['plan']==x['plan'] and content['contract_sha256']==digest(manifest['contract'])
    assert ev['input_refs']['artifact_sha256']==artifact['sha256']
    expected_input='\n'.join(f"{i} {cases[i]['text']}" for i in item['case_ids'])
    assert manifest['contract']['source_text']==expected_input
    matches=re.findall(r'(?im)^\s*(Q\d{2})\s*\|\s*(T[1-8])\s*$',content['steps'][0]['text'])
    count=Counter(q for q,_ in matches);lookup=dict(matches)
    for q in item['case_ids']:
        value=codes.get(lookup.get(q)) if count[q]==1 else None
        assert cases[q].get('workos_prediction')==value
        seen.append(q)
assert len(seen)==40 and len(set(seen))==40
print('Verified upstream hashes, 40 sampled cases, TF-IDF/SVM baseline, 4 context/artifact hashes and output mappings.')
print('Baseline correct:',sum(c['expected']==c['baseline_prediction'] for c in x['cases']))
print('Work OS correct:',sum(c['expected']==c.get('workos_prediction') for c in x['cases']))
