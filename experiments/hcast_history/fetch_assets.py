"""Download and verify only HCAST's DVC TeX/bibliography assets to a caller-specified folder."""
import hashlib, json, os, urllib.request
from pathlib import Path
ROOT=Path(os.environ['HCAST_TEX_ASSETS']);BASE='https://production-task-assets-public.s3.amazonaws.com/611138f2-ed03-4565-a230-428602554f70/files/md5/'
RAW=urllib.request.urlopen(BASE+'fc/e1068466d17863c10efa5377b9b189.dir',timeout=60).read()
if hashlib.md5(RAW).hexdigest()!='fce1068466d17863c10efa5377b9b189':raise RuntimeError('DVC directory manifest hash mismatch')
count=0
for entry in json.loads(RAW):
 path=Path(entry['relpath'])
 if path.suffix not in ('.tex','.bbl'):continue
 if path.is_absolute() or '..' in path.parts:raise RuntimeError('unsafe path')
 md5=entry['md5'];data=urllib.request.urlopen(BASE+md5[:2]+'/'+md5[2:],timeout=60).read()
 if hashlib.md5(data).hexdigest()!=md5:raise RuntimeError('asset hash mismatch: '+str(path))
 target=ROOT/path;target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and target.read_bytes()!=data:raise RuntimeError('refuse to overwrite '+str(target))
 target.write_bytes(data);count+=1
print('Verified DVC manifest and downloaded',count,'TeX/bibliography assets')
