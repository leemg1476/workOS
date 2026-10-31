"""Fixed synthetic classification checks, not an assessment of policy meaning."""
from sqlalchemy import text
from app.work.market import digest
from app.work.voc import classify

SUITES = ('voc-suite-v1','voc-regression-v1')
SCOPE = 'FIXED_VOC_CLASSIFICATION_ONLY_NOT_POLICY_SEMANTICS'


def load_inputs(conn):
    state = conn.execute(text("SELECT active_method_id,version FROM voc_method_state WHERE task_id='TASK-003'" )).mappings().one()
    methods = {}
    for label,method_id in (('baseline','champion-v1'),('active',state['active_method_id'])):
        row = conn.execute(text('SELECT method_id,rules,sha256 FROM voc_method_versions WHERE method_id=:id'),{'id':method_id}).mappings().one()
        methods[label] = dict(row)
        if digest(row['rules']) != row['sha256']:
            raise ValueError('method hash mismatch')
    suites = []
    for name in SUITES:
        meta = conn.execute(text('SELECT suite_version,case_count,sha256 FROM voc_eval_suites WHERE suite_version=:id'),{'id':name}).mappings().one()
        cases = [dict(row) for row in conn.execute(text('''SELECT case_id,body,expected_category FROM voc_eval_cases
            WHERE suite_version=:id ORDER BY case_id'''),{'id':name}).mappings()]
        if len(cases) != meta['case_count'] or len(cases) < (20 if name == SUITES[0] else 6) or digest(cases) != meta['sha256']:
            raise ValueError('fixed evaluation suite missing or changed')
        suites.append({'suite_version':name,'sha256':meta['sha256'],'cases':cases})
    return {'state':dict(state),'baseline':methods['baseline'],'active':methods['active'],'suites':suites}


def make_report(manifest):
    inputs = manifest['evaluation_inputs']
    suites = []
    for suite in inputs['suites']:
        cases = []
        for case in suite['cases']:
            old = classify(case['body'],inputs['baseline']['rules'])
            active = classify(case['body'],inputs['active']['rules'])
            expected = case['expected_category']
            cases.append({'case_id':case['case_id'],'expected':expected,'baseline':old,'active':active})
        losses = [case['case_id'] for case in cases if case['baseline']==case['expected'] and case['active']!=case['expected']]
        suites.append({'suite_version':suite['suite_version'],'sha256':suite['sha256'],'case_count':len(cases),
                       'baseline_correct':sum(case['baseline']==case['expected'] for case in cases),
                       'active_correct':sum(case['active']==case['expected'] for case in cases),
                       'regressions':losses,'cases':cases})
    return {'task_id':'TASK-002','data_mode':'DEMO DATA','step_no':3,'scope':SCOPE,
            'predecessor_artifact_id':manifest['predecessor_artifact_id'],
            'document_id':manifest['document']['id'],'document_sha256':manifest['document']['sha256'],
            'baseline_method':inputs['baseline']['method_id'],'active_method':inputs['active']['method_id'],
            'active_method_state_version':inputs['state']['version'],'suites':suites}
