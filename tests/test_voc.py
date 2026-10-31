from fastapi.testclient import TestClient
from sqlalchemy import text
from app.api.main import app
from app.work.db import engine
from app.work.seed import seed
from app.worker import process_claim


def run_voc(kernel, key):
    work,_ = kernel.trigger('TASK-003-JOB',key,{})
    run = kernel.claim()
    assert run['work_id'] == work
    result = process_claim(kernel,run)
    assert result['passed']
    return work,run['run_id'],result['artifact_id']


def post_action(client, proposal, action, key):
    return client.post(f'/api/improvements/{proposal}/{action}',json={'idempotency_key':key})


def feedback(client, artifact, key, candidate='challenger-v2'):
    response = client.post('/api/feedback',json={'idempotency_key':key,'artifact_id':artifact,
        'recommendation_id':'REC-billing','reason':'누락','challenger_version':candidate})
    assert response.status_code == 200
    return response.json()['proposal_id']


def find_proposal(client, proposal):
    return next(p for p in client.get('/api/improvements').json() if p['proposal_id']==proposal)


def test_feedback_paired_evaluation_explicit_apply_and_rollback(kernel):
    seed(engine)
    work,run,artifact = run_voc(kernel,'voc-run-1')
    with TestClient(app) as client:
        initial = client.get(f'/api/runs/{run}/artifacts').json()[0]
        report = initial['content']
        assert report['method_version']=='champion-v1' and report['data_mode']=='DEMO DATA'
        assert [r['count'] for r in report['recommendations']] == [6,6,6]
        assert len(report['unclassified_ids']) == 6 and initial['evidence'][0]['result']['passed']
        proposal = feedback(client,artifact,'feedback-1')
        assert feedback(client,artifact,'feedback-1') == proposal
        assert post_action(client,proposal,'apply','too-early').status_code == 409
        assert post_action(client,proposal,'evaluate','evaluate-1').status_code == 200
        result = find_proposal(client,proposal)
        assert result['status']=='PROPOSED' and result['verdict']=='PASS'
        assert result['results']['case_count']==24 and result['results']['champion_correct']==18
        assert result['results']['challenger_correct']==24 and not result['results']['regressions']
        assert len(result['results']['cases']) == 24 and len(result['results']['gains']) == 6
        assert result['results']['regression_suite']['case_count'] == 6
        assert result['results']['regression_suite']['champion_correct'] == 3
        assert result['results']['regression_suite']['challenger_correct'] == 6
        assert not result['results']['regression_suite']['regressions']
        assert client.get('/api/voc/state').json()['method']['active_method_id']=='champion-v1'
        assert post_action(client,proposal,'apply','apply-1').status_code == 200
        assert post_action(client,proposal,'apply','apply-1').json()['changed'] is False
        assert client.get('/api/voc/state').json()['method']['active_method_id']=='challenger-v2'
        stale_feedback = client.post('/api/feedback',json={'idempotency_key':'wrong-method','artifact_id':artifact,
            'recommendation_id':'REC-billing','reason':'누락','challenger_version':'challenger-regressed'})
        assert stale_feedback.status_code == 409
        _,second,second_artifact = run_voc(kernel,'voc-run-2')
        changed = client.get(f'/api/runs/{second}/artifacts').json()[0]['content']
        assert changed['method_version']=='challenger-v2'
        assert [r['count'] for r in changed['recommendations']] == [8,8,8] and not changed['unclassified_ids']
        assert post_action(client,proposal,'rollback','rollback-1').status_code == 200
        assert client.get('/api/voc/state').json()['method']['active_method_id']=='champion-v1'
        _,third,_ = run_voc(kernel,'voc-run-3')
        assert client.get(f'/api/runs/{third}/artifacts').json()[0]['content']['method_version']=='champion-v1'
        assert find_proposal(client,proposal)['status']=='ROLLED_BACK'


def test_regressed_challenger_cannot_apply_and_can_be_rejected(kernel):
    seed(engine)
    _,_,artifact = run_voc(kernel,'bad-voc-run')
    with TestClient(app) as client:
        proposal = feedback(client,artifact,'bad-feedback','challenger-regressed')
        assert post_action(client,proposal,'evaluate','bad-eval').status_code == 200
        result = find_proposal(client,proposal)
        assert result['verdict']=='FAIL' and len(result['results']['regressions'])==3
        assert len(result['results']['regression_suite']['regressions']) == 1
        assert post_action(client,proposal,'apply','bad-apply').status_code == 409
        assert post_action(client,proposal,'reject','bad-reject').status_code == 200
        assert find_proposal(client,proposal)['status']=='REJECTED'
        assert client.get('/api/voc/state').json()['method']['active_method_id']=='champion-v1'


def test_separate_regression_suite_blocks_overfit_candidate(kernel):
    seed(engine)
    _,_,artifact = run_voc(kernel,'overfit-voc-run')
    with TestClient(app) as client:
        proposal = feedback(client,artifact,'overfit-feedback','challenger-overfit')
        assert post_action(client,proposal,'evaluate','overfit-eval').status_code == 200
        result = find_proposal(client,proposal)
        assert result['results']['challenger_correct'] == 24
        assert not result['results']['regressions']
        assert len(result['results']['regression_suite']['regressions']) == 2
        assert result['verdict']=='FAIL'
        assert post_action(client,proposal,'apply','overfit-apply').status_code == 409


def test_changed_eval_suite_and_stale_champion_block_promotion(kernel):
    seed(engine)
    _,_,artifact = run_voc(kernel,'stable-voc')
    with TestClient(app) as client:
        one = feedback(client,artifact,'feedback-first')
        two = feedback(client,artifact,'feedback-second')
        assert post_action(client,one,'evaluate','eval-first').status_code == 200
        assert post_action(client,two,'evaluate','eval-second').status_code == 200
        assert post_action(client,one,'apply','apply-first').status_code == 200
        assert post_action(client,two,'apply','apply-stale').status_code == 409
        assert post_action(client,one,'rollback','rollback-first').status_code == 200
        assert post_action(client,two,'apply','apply-stale-after-rollback').status_code == 409
        three = feedback(client,artifact,'feedback-third')
        with engine.begin() as c:
            c.execute(text("INSERT INTO voc_eval_cases(suite_version,case_id,body,expected_category) VALUES ('voc-suite-v1','TAMPER','결제','billing')"))
        assert post_action(client,three,'evaluate','changed-suite').status_code == 409


def test_source_change_invalidates_feedback_and_prior_proposals(kernel):
    seed(engine)
    _,_,artifact = run_voc(kernel,'voc-before-policy-change')
    with TestClient(app) as client:
        evaluated = feedback(client,artifact,'source-feedback-1')
        draft = feedback(client,artifact,'source-feedback-2')
        assert post_action(client,evaluated,'evaluate','source-eval').status_code == 200
        versions = client.get('/api/policies/POLICY-VOC').json()['versions']
        payload = {'event_id':'policy-invalidates-proposals','document_id':'POLICY-VOC',
                   'old_version':versions[0]['version'],'new_version':versions[1]['version'],
                   'old_sha256':versions[0]['sha256'],'new_sha256':versions[1]['sha256']}
        assert client.post('/api/events/document.updated',json=payload).status_code == 200
        assert post_action(client,evaluated,'apply','source-stale-apply').status_code == 409
        assert post_action(client,draft,'evaluate','source-stale-eval').status_code == 409
        stale_feedback = client.post('/api/feedback',json={'idempotency_key':'source-stale-feedback','artifact_id':artifact,
            'recommendation_id':'REC-billing','reason':'누락','challenger_version':'challenger-v2'})
        assert stale_feedback.status_code == 409


def test_changed_regression_suite_blocks_apply(kernel):
    seed(engine)
    _,_,artifact = run_voc(kernel,'voc-regression-version')
    with TestClient(app) as client:
        proposal = feedback(client,artifact,'regression-version-feedback')
        assert post_action(client,proposal,'evaluate','regression-version-eval').status_code == 200
        with engine.begin() as c:
            c.execute(text("INSERT INTO voc_eval_cases(suite_version,case_id,body,expected_category) VALUES ('voc-regression-v1','EXTRA','결제','billing')"))
        assert post_action(client,proposal,'apply','regression-version-apply').status_code == 409
