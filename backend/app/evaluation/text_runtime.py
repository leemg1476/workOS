"""Resolve experiment inputs only from a persisted, authorized evaluation work mapping."""
from copy import deepcopy
from sqlalchemy import text
from app.work.market import digest
from app.work.service import Conflict, canonical
from app.evaluation.text_regression import verify_suite


def effective_contract(c, work_id, task_id, task_version, base, *, allow_evaluated=False):
    row=c.execute(text('''SELECT x.*,m.case_id,m.side,s.version AS suite_version,s.snapshot AS suite_snapshot,s.snapshot_sha256
        FROM text_experiment_work m JOIN text_experiments x ON x.experiment_id=m.experiment_id
        JOIN text_regression_suites s ON s.suite_id=x.suite_id WHERE m.work_id=CAST(:id AS uuid)'''),{'id':str(work_id)}).mappings().first()
    if row is None:
        pinned=c.execute(text('''SELECT m.task_id,m.source_version,s.source_text,s.source_sha256
            FROM text_source_work m JOIN text_source_versions s ON s.task_id=m.task_id AND s.version=m.source_version
            WHERE m.work_id=CAST(:id AS uuid)'''),{'id':str(work_id)}).mappings().first()
        if pinned is None:
            created_after_migration=c.execute(text('''SELECT w.created_at >= m.applied_at FROM work_items w
                JOIN schema_migrations m ON m.version='017_text_sources' WHERE w.work_id=CAST(:id AS uuid)'''),
                {'id':str(work_id)}).scalar_one_or_none()
            if created_after_migration:
                raise Conflict('new text work has no pinned source')
            return base, None
        if pinned['task_id']!=task_id or digest(pinned['source_text'])!=pinned['source_sha256']:
            raise Conflict('pinned source changed')
        contract=deepcopy(base)
        contract['source_text']=pinned['source_text']
        from app.workloads.text_links import linked_source
        linked=linked_source(c,work_id)
        work=c.execute(text('SELECT input_snapshot FROM work_items WHERE work_id=:id'),{'id':work_id}).scalar_one()
        if work.get('source')=='accepted_work_link' and linked is None:
            raise Conflict('linked work source record is missing')
        if linked and work.get('source')!='accepted_work_link':
            raise Conflict('linked work source marker changed')
        if linked:
            # Preserve the complete accepted artifact and its identity as untrusted source data.
            contract['linked_source']=linked
            contract['source_text']=pinned['source_text']+'\n\n[사람이 채택한 선행 업무 결과 · 출처와 내용]\n'+canonical(linked)
        return contract, None
    if (row['status'] not in (('RUNNING','EVALUATED') if allow_evaluated else ('RUNNING',)) or row['task_id']!=task_id or row['champion_version']!=task_version
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
