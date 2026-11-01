"""Resolve experiment inputs only from a persisted, authorized evaluation work mapping."""
from copy import deepcopy
from sqlalchemy import text
from app.work.market import digest
from app.work.service import Conflict
from app.evaluation.text_regression import verify_suite


def effective_contract(c, work_id, task_id, task_version, base):
    row=c.execute(text('''SELECT x.*,m.case_id,m.side,s.version AS suite_version,s.snapshot AS suite_snapshot,s.snapshot_sha256
        FROM text_experiment_work m JOIN text_experiments x ON x.experiment_id=m.experiment_id
        JOIN text_regression_suites s ON s.suite_id=x.suite_id WHERE m.work_id=CAST(:id AS uuid)'''),{'id':str(work_id)}).mappings().first()
    if row is None:
        return base, None
    if (row['status']!='RUNNING' or row['task_id']!=task_id or row['champion_version']!=task_version
        or digest(base)!=row['champion_contract_sha256'] or digest(row['candidate_plan'])!=row['candidate_plan_sha256']
        or row['suite_sha256']!=row['snapshot_sha256']):
        raise Conflict('experiment contract or work mapping changed')
    suite={'task_id':row['task_id'],'version':row['suite_version'],'snapshot':row['suite_snapshot'],'snapshot_sha256':row['snapshot_sha256']}
    snapshot=verify_suite(c,task_id,suite)
    entry=next((x for x in snapshot['cases'] if x['case_id']==str(row['case_id'])),None)
    if entry is None or row['side'] not in ('CHAMPION','CHALLENGER'):
        raise Conflict('experiment case missing from suite')
    contract=deepcopy(base)
    if row['side']=='CHALLENGER':
        contract['plan']=row['candidate_plan']
    contract['source_text']=entry['case']['input_text']
    scope={'experiment_id':str(row['experiment_id']),'suite_sha256':row['suite_sha256'],
           'case_id':entry['case_id'],'case_sha256':entry['case_sha256'],'side':row['side']}
    return contract,scope
