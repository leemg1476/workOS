"""Governed, synthetic-only A/B/C context projections for the comparison harness."""
from copy import deepcopy
from sqlalchemy import text
from app.domain.hashing import digest,canonical
from app.work.service import Conflict,one


def freeze_work(kernel,c,job,work_id):
    from app.work.task_memory import build
    work=one(c,'SELECT * FROM work_items WHERE work_id=:id',{'id':work_id});marker=work['input_snapshot']
    if 'memory_study_id' not in marker:return
    study=one(c,'SELECT * FROM memory_comparison_studies WHERE study_id=CAST(:id AS uuid)',{'id':marker['memory_study_id']})
    if not study or digest(study['snapshot'])!=study['snapshot_sha256']:raise Conflict('comparison study missing or changed')
    config=study['snapshot'];arm=marker.get('arm');phase=marker.get('phase');case_id=marker.get('case_id')
    if config.get('scope')!='SYNTHETIC_MEMORY_COMPARISON_NOT_PRODUCTION' or config['tasks'].get(arm)!=work['task_id']:
        raise Conflict('task is not authorized for this study arm')
    case=next((v for v in config['cases'] if v['case_id']==case_id and v['phase']==phase),None)
    pinned=one(c,'''SELECT s.source_text FROM text_source_work w JOIN text_source_versions s
        ON s.task_id=w.task_id AND s.version=w.source_version WHERE w.work_id=:id''',{'id':work_id})
    if not case or not pinned or pinned['source_text']!=case['source_text']:raise Conflict('study case input is not the frozen case')
    task=one(c,'SELECT output_schema FROM task_definitions WHERE task_id=:id AND version=:v',{'id':work['task_id'],'v':work['task_version']})
    if digest(task['output_schema']['plan'])!=config['plan_sha256'] or task['output_schema'].get('execution_options')!=config['execution_options']:
        raise Conflict('study workflow or tool budget changed')
    history=build(c,work['task_id'],work['task_version'])
    if arm=='A':
        history={k:deepcopy(v) for k,v in history.items() if k not in ('accepted_results','draft_results','failures','reviews','source_library')}
        history.update(accepted_results=[],draft_results=[],failures=[],reviews=[],source_library=[])
    if arm in ('A','B'):
        history['version']='task-memory-v3';history.pop('procedure_binding',None)
    common=deepcopy(config['common_contract_memory'])
    learned=c.execute(text('SELECT * FROM memory_study_learning WHERE study_id=:id AND arm=:arm ORDER BY round_no'),{'id':study['study_id'],'arm':arm}).mappings().all()
    for learning in learned:
        if digest(learning['snapshot'])!=learning['snapshot_sha256']:raise Conflict('study learning record changed')
        origin=one(c,'''SELECT m.* FROM memory_study_work m JOIN artifacts a ON a.run_id IN
            (SELECT run_id FROM runs WHERE work_id=m.work_id) WHERE m.work_id=CAST(:work AS uuid) AND a.artifact_id=CAST(:artifact AS uuid)''',
            {'work':learning['snapshot'].get('origin_work_id'),'artifact':learning['snapshot'].get('origin_artifact_id')})
        if not origin or str(origin['study_id'])!=str(study['study_id']) or origin['arm']!=arm or origin['phase']!='TRAIN':
            raise Conflict('learning may use only this arm training episodes, never final heldout outputs')
        if arm=='C':
            from app.evaluation.knowledge_experiments import require_evaluation
            for proposed in learning['snapshot']['entries']:
                entry=one(c,'SELECT * FROM task_knowledge WHERE entry_id=CAST(:id AS uuid)',{'id':proposed['entry_id']})
                if not entry or entry['kind']!='RULE' or entry['sha256']!=proposed['sha256']:raise Conflict('compiled study rule is not the evaluated candidate')
                require_evaluation(c,entry,learning['snapshot'].get('evaluation_id'),allow_synthetic_fixture=True)
        elif arm=='B':
            event=one(c,"SELECT payload FROM work_events WHERE event_id=CAST(:id AS uuid) AND event_type='work.agent_feedback'",{'id':learning['snapshot'].get('feedback_event_id')})
            allowed=(event['payload'].get('knowledge_to_retain') or [event['payload']['note']]) if event else []
            if not event or event['payload']['work_id']!=learning['snapshot']['origin_work_id'] or any(e['kind']!='LESSON' or e['payload']['text'] not in allowed for e in learning['snapshot']['entries']):
                raise Conflict('legacy study feedback must retain its actual training provenance')
        common.extend(deepcopy(learning['snapshot']['entries']))
    history['knowledge']={'version':'memory-study-v1','active':common,'excluded':[],'sha256':digest(common)}
    history['study']={'study_id':str(study['study_id']),'arm':arm,'phase':phase,'case_id':case_id,
        'scope':'EXPERIMENTAL_MEMORY_VARIANT_NOT_USER_APPROVED_PRODUCTION','learning_record_hashes':[r['snapshot_sha256'] for r in learned]}
    c.execute(text('''INSERT INTO memory_study_work(work_id,study_id,arm,phase,case_id,snapshot,snapshot_sha256)
        VALUES (:work,:study,:arm,:phase,:case,CAST(:snapshot AS jsonb),:sha)'''),
        {'work':work_id,'study':study['study_id'],'arm':arm,'phase':phase,'case':case_id,'snapshot':canonical(history),'sha':digest(history)})
    kernel._event(c,'memory.study_context_frozen',work_id,{'study_id':str(study['study_id']),'arm':arm,'phase':phase,'case_id':case_id,'memory_sha256':digest(history)})


def pin(c,work,memory):
    row=one(c,'SELECT * FROM memory_study_work WHERE work_id=:id',{'id':work['work_id']})
    if not row:return memory
    if digest(row['snapshot'])!=row['snapshot_sha256']:raise Conflict('study memory changed')
    result=deepcopy(row['snapshot'])
    # Execution-local context is pinned at claim time, never borrowed from an
    # earlier Work's environment or failure history.
    if 'execution_context' in memory:result['execution_context']=memory['execution_context']
    return result
