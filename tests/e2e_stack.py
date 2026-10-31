"""Three-scenario E2E against a running *disposable* Compose project and real worker."""
import os
import time
import uuid
import httpx

if os.getenv('WORKOS_E2E_CONFIRM') != 'disposable':
    raise SystemExit('Use a disposable, freshly seeded Compose project and set WORKOS_E2E_CONFIRM=disposable')

base = os.getenv('WORKOS_E2E_BASE','http://localhost:8000/api')
client = httpx.Client(base_url=base,timeout=10)


def key():
    return str(uuid.uuid4())


def get(path, **kwargs):
    response=client.get(path,**kwargs)
    response.raise_for_status()
    return response.json()


def post(path, payload):
    response=client.post(path,json=payload)
    response.raise_for_status()
    return response.json()


def wait_work(work_id):
    deadline=time.monotonic()+75
    while time.monotonic()<deadline:
        data=get('/work/'+work_id)
        if data['work']['status'] in ('COMMITTED','REWORK_REQUIRED'):
            return data
        time.sleep(.5)
    raise AssertionError(f'work {work_id} did not finish')


def wait_policy_method(root, method, previous_run):
    deadline=time.monotonic()+75
    while time.monotonic()<deadline:
        node=get('/policy-work/'+root)['followups'][2]
        if node['status']=='COMMITTED' and node['run_id'] != previous_run:
            artifacts=get('/runs/'+node['run_id']+'/artifacts')
            if artifacts and artifacts[-1]['content']['active_method']==method:
                return node['run_id']
        time.sleep(.5)
    raise AssertionError(f'policy regression did not revalidate using {method}')


def launch(task, snapshot=None):
    work=post(f'/tasks/{task}/run',{'idempotency_key':key(),'input_snapshot':snapshot or {}})['work_id']
    return work,wait_work(work)


def result(data):
    run=data['runs'][-1]
    steps=get(f"/runs/{run['run_id']}")['steps']
    assert [(s['node_name'],s['status']) for s in steps]==[
        ('inspect_context','STARTED'),('inspect_context','SUCCEEDED'),
        ('draft','STARTED'),('draft','SUCCEEDED'),('finalize','STARTED'),('finalize','SUCCEEDED')]
    artifacts=get(f"/runs/{run['run_id']}/artifacts")
    assert artifacts, f"missing artifact: {run['status']} {run['error']}"
    return run,artifacts[-1]


def action(proposal, name):
    return post(f'/improvements/{proposal}/{name}',{'idempotency_key':key()})


def proposal(proposal_id):
    return next(x for x in get('/improvements') if x['proposal_id']==proposal_id)


def create_feedback(artifact, candidate):
    return post('/feedback',{'idempotency_key':key(),'artifact_id':artifact,'recommendation_id':'REC-billing',
                              'reason':'누락','challenger_version':candidate})['proposal_id']


def main():
    deadline=time.monotonic()+120
    while True:
        try:
            health=get('/health')
            if health['data_mode']=='DEMO DATA' and health['search']['status']=='connected':
                break
        except (httpx.HTTPError,KeyError):
            pass
        if time.monotonic()>=deadline:
            raise AssertionError('API or demo OpenSearch index did not become ready')
        time.sleep(1)
    policy=get('/policies/POLICY-VOC')
    assert policy['dependencies'][0]['status']=='CURRENT' and policy['latest_event'] is None
    assert get('/voc/state')['method']['active_method_id']=='champion-v1'

    _,first=launch('TASK-001',{'dataset_version':'v1','model_mode':'MOCK'})
    _,second=launch('TASK-001',{'dataset_version':'v2','model_mode':'MOCK'})
    first_run,first_art=result(first)
    second_run,second_art=result(second)
    assert first['work']['status']==second['work']['status']=='COMMITTED'
    assert first_art['evidence'][0]['result']['passed'] and second_art['evidence'][0]['result']['passed']
    context=get('/runs/'+second_run['run_id'])['context']['manifest']
    assert context['retrieval']['mode']=='OPENSEARCH' and context['sql_source_mode']=='POSTGRES_READ_ONLY_ROLE'
    delta=get('/tasks/TASK-001/compare',params={'left':first_run['run_id'],'right':second_run['run_id']})
    assert next(x for x in delta['changes'] if x['symbol']=='USD/KRW')['difference']=='30'
    broken_id,broken=launch('TASK-001',{'dataset_version':'v2','model_mode':'MOCK','omit_document':True})
    assert broken['runs'][-1]['status']=='VERIFICATION_FAILED' and broken['work']['accepted_artifact_id'] is None
    post(f'/work/{broken_id}/retry',{'idempotency_key':key()})
    repaired=wait_work(broken_id)
    assert [r['status'] for r in repaired['runs']]==['VERIFICATION_FAILED','SUCCEEDED']
    print('TASK-001: v1/v2 delta 30; missing citation failed then retry succeeded',flush=True)

    old,new=policy['versions']
    event={'event_id':key(),'document_id':old['document_id'],'old_version':old['version'],'new_version':new['version'],
           'old_sha256':old['sha256'],'new_sha256':new['sha256']}
    created=post('/events/document.updated',event)
    duplicate=post('/events/document.updated',event)
    assert created['created'] and not duplicate['created'] and duplicate['work_id']==created['work_id']
    change=wait_work(created['work_id'])
    assert change['work']['status']=='COMMITTED'
    result(change)
    deadline=time.monotonic()+75
    while True:
        details=get('/policy-work/'+created['work_id'])
        if details['followups'][2]['status'] in ('COMMITTED','REWORK_REQUIRED'):
            break
        if time.monotonic()>=deadline:
            raise AssertionError('policy follow-up did not finish')
        time.sleep(.5)
    assert details['retrieval']['mode']=='OPENSEARCH' and details['artifact']['verification']['passed']
    assert [x['status'] for x in details['followups']]==['COMMITTED','COMMITTED','COMMITTED','WAITING_APPROVAL']
    assert details['followups'][0]['verification']['passed']
    assert details['followups'][1]['verification']['passed']
    assert details['followups'][1]['verification']['indexed_document']['id']=='POLICY-VOC@2'
    assert details['followups'][2]['verification']['passed']
    assert details['followups'][2]['verification']['scope']=='FIXED_VOC_CLASSIFICATION_ONLY_NOT_POLICY_SEMANTICS'
    assert [suite['case_count'] for suite in details['followups'][2]['verification']['suite_results']]==[24,6]
    review={'idempotency_key':key(),'artifact_id':details['artifact']['artifact_id'],'note':'가상 규정 검토 메모'}
    assert post('/policy-work/'+created['work_id']+'/review-notes',review)['recorded']
    assert not post('/policy-work/'+created['work_id']+'/review-notes',review)['recorded']
    assert get('/policy-work/'+created['work_id'])['review_notes'][0]['payload']['decision']=='NOTE_ONLY'
    review_context=get('/policy-work/'+created['work_id']+'/review-context')
    assert review_context['ready'] and len(review_context['context']['stages'])==4
    decision={'idempotency_key':key(),'context_sha256':review_context['context_sha256'],
              'decision':'REVIEWED','note':'가상 문서와 검사 결과 검토'}
    assert post('/policy-work/'+created['work_id']+'/review-decision',decision)['recorded']
    assert not post('/policy-work/'+created['work_id']+'/review-decision',decision)['recorded']
    reviewed=get('/policy-work/'+created['work_id'])
    assert reviewed['followups'][3]['status']=='COMMITTED' and reviewed['reviews'][0]['is_current']
    assert reviewed['reviews'][0]['content']['policy_applied'] is False
    assert get('/policies/POLICY-VOC')['dependencies'][0]['status']=='REVIEW_REQUIRED'
    print('TASK-002: event idempotent; three checks and explicit human review recorded without applying policy',flush=True)

    _,initial=launch('TASK-003')
    run,artifact=result(initial)
    assert artifact['content']['method_version']=='champion-v1' and len(artifact['content']['unclassified_ids'])==6
    assert artifact['content']['previous_artifact_id'] is None
    assert {r['comparison'] for r in artifact['content']['recommendations']}=={'NO_PREVIOUS_REPORT'}
    assert artifact['content']['retrieval_mode']=='OPENSEARCH'
    assert all(len(r['document_citations'])==1 for r in artifact['content']['recommendations'])
    assert artifact['evidence'][0]['result']['checks']['documents_search_current']
    voc_context=get('/runs/'+run['run_id'])['context']['manifest']
    assert voc_context['retrieval']['mode']=='OPENSEARCH' and voc_context['sql_source_mode']=='POSTGRES_READ_ONLY_ROLE'
    assert voc_context['document_parameters']['version']==1 and len(voc_context['documents'])==3
    proposal_id=create_feedback(artifact['artifact_id'],'challenger-v2')
    action(proposal_id,'evaluate')
    evaluation=proposal(proposal_id)
    assert evaluation['verdict']=='PASS' and evaluation['results']['champion_correct']==18 and evaluation['results']['challenger_correct']==24
    assert evaluation['results']['regression_suite']['case_count']==6 and not evaluation['results']['regression_suite']['regressions']
    assert get('/voc/state')['method']['active_method_id']=='champion-v1'
    prior_policy_run=details['followups'][2]['run_id']
    action(proposal_id,'apply')
    applied_policy_run=wait_policy_method(created['work_id'],'challenger-v2',prior_policy_run)
    changed_review=get('/policy-work/'+created['work_id'])
    assert changed_review['followups'][3]['status']=='WAITING_APPROVAL' and not changed_review['reviews'][0]['is_current']
    _,changed=launch('TASK-003')
    _,changed_artifact=result(changed)
    assert changed_artifact['content']['method_version']=='challenger-v2' and not changed_artifact['content']['unclassified_ids']
    assert changed_artifact['content']['previous_artifact_id']==artifact['artifact_id']
    assert {r['comparison'] for r in changed_artifact['content']['recommendations']}=={'SAME_CATEGORY'}
    assert changed_artifact['evidence'][0]['result']['checks']['previous_artifact_verified']
    action(proposal_id,'rollback')
    rolled_back_policy_run=wait_policy_method(created['work_id'],'champion-v1',applied_policy_run)
    review_context=get('/policy-work/'+created['work_id']+'/review-context')
    assert review_context['ready']
    assert post('/policy-work/'+created['work_id']+'/review-decision',{'idempotency_key':key(),
        'context_sha256':review_context['context_sha256'],'decision':'CHANGES_REQUESTED','note':'가상 보완 요청'})['recorded']
    assert get('/policy-work/'+created['work_id'])['reviews'][0]['content']['decision']=='CHANGES_REQUESTED'
    assert get('/policies/POLICY-VOC')['dependencies'][0]['status']=='REVIEW_REQUIRED'
    assert post('/work/'+created['work_id']+'/revalidate',{'idempotency_key':key()})['queued']
    wait_policy_method(created['work_id'],'champion-v1',rolled_back_policy_run)
    reviewed_again=get('/policy-work/'+created['work_id'])
    assert reviewed_again['followups'][3]['status']=='WAITING_APPROVAL'
    assert not any(item['is_current'] for item in reviewed_again['reviews'])
    _,restored=launch('TASK-003')
    _,restored_artifact=result(restored)
    assert restored_artifact['content']['method_version']=='champion-v1'
    assert restored_artifact['content']['previous_artifact_id']==changed_artifact['artifact_id']
    bad=create_feedback(restored_artifact['artifact_id'],'challenger-overfit')
    action(bad,'evaluate')
    bad_result=proposal(bad)
    assert bad_result['results']['challenger_correct']==24 and bad_result['verdict']=='FAIL'
    assert len(bad_result['results']['regression_suite']['regressions'])==2
    assert client.post(f'/improvements/{bad}/apply',json={'idempotency_key':key()}).status_code==409
    action(bad,'reject')
    assert proposal(bad)['status']=='REJECTED'
    print('TASK-003: previous verified recommendations compared by category; 18/24 vs 24/24; apply, rollback, overfit rejected',flush=True)


if __name__=='__main__':
    main()
