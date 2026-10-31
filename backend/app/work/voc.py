"""Deterministic synthetic VOC workload and a fixed, labeled demo evaluation suite."""
import re
from pydantic import ValidationError
from app.domain.contracts import VocReport, VocReportWithHistory
from app.work.market import digest

CATEGORIES = ('billing','delivery','support')
ACTIONS = {'billing':'결제·환불 안내의 누락 구간을 점검한다.',
           'delivery':'배송 상태와 도착 안내를 점검한다.',
           'support':'고객 문의와 상담 응대 경로를 점검한다.'}
METHODS = {
    'champion-v1': {'billing':['결제','청구'],'delivery':['배송','택배'],'support':['상담','응대']},
    'challenger-v2': {'billing':['결제','청구','환불'],'delivery':['배송','택배','도착'],'support':['상담','응대','문의']},
    'challenger-regressed': {'billing':['결제','청구'],'delivery':['택배','도착'],'support':['상담','응대']},
    'challenger-overfit': {'billing':['결제','청구','환불','별도'],'delivery':['배송','택배','도착'],'support':['상담','응대','문의']},
}
TERMS = {'billing':['결제','청구','결제','청구','결제','청구','환불','환불'],
         'delivery':['배송','택배','배송','택배','배송','택배','도착','도착'],
         'support':['상담','응대','상담','응대','상담','응대','문의','문의']}


def fixture_records():
    rows = []
    for category in CATEGORIES:
        for index, term in enumerate(TERMS[category],1):
            rows.append({'record_id':f'VOC-{category.upper()}-{index:02d}', 'source_version':'voc-demo-v1',
                         'body':f'가상 고객이 {term} 안내를 이해하기 어렵다고 남김 {index}.'})
    return sorted(rows,key=lambda x:x['record_id'])


def fixture_cases():
    cases = []
    for category in CATEGORIES:
        for index, term in enumerate(TERMS[category],1):
            cases.append({'case_id':f'EVAL-{category.upper()}-{index:02d}',
                          'body':f'고정 평가 예시 {index}: {term} 관련 안내가 불편합니다.', 'expected_category':category})
    return sorted(cases,key=lambda x:x['case_id'])


def fixture_regression_cases():
    terms = [('billing','결제'),('billing','환불'),('delivery','배송'),('delivery','도착'),('support','상담'),('support','문의')]
    return sorted([{'case_id':f'REGRESSION-{category.upper()}-{i:02d}',
                    'body':f'별도 회귀 사례 {i}: {term} 안내가 어려웠습니다.', 'expected_category':category}
                   for i,(category,term) in enumerate(terms,1)],key=lambda x:x['case_id'])


def classify(body, rules):
    for category in CATEGORIES:
        if any(term in body for term in rules.get(category, [])):
            return category
    return 'unclassified'


def make_report(manifest):
    buckets = {category:[] for category in CATEGORIES}
    unknown = []
    for row in manifest['records']:
        category = classify(row['body'],manifest['method']['rules'])
        (buckets[category] if category in buckets else unknown).append(row['record_id'])
    recommendations = [{'recommendation_id':f'REC-{category}', 'category':category, 'count':len(ids),
                        'record_ids':ids,'action':ACTIONS[category]} for category,ids in buckets.items() if ids]
    recommendations.sort(key=lambda x:(-x['count'],x['category']))
    report = {'task_id':'TASK-003','data_mode':'DEMO DATA','model_mode':'MOCK','source_version':'voc-demo-v1',
            'method_version':manifest['method']['method_id'],'recommendations':recommendations[:3],
            'unclassified_ids':unknown,'source_review_status':manifest['source_review_status']}
    if manifest['task_version'] >= 3:
        previous = manifest['previous_report']
        previous_by_category = {item['category']:item['recommendation_id'] for item in previous['content']['recommendations']} if previous else {}
        for item in report['recommendations']:
            item['previous_recommendation_id'] = previous_by_category.get(item['category'])
            item['comparison'] = ('NO_PREVIOUS_REPORT' if previous is None else
                                  'SAME_CATEGORY' if item['previous_recommendation_id'] else 'NEW_CATEGORY')
        report.update(previous_artifact_id=previous['artifact_id'] if previous else None,
                      history_as_of=manifest['history_as_of'],comparison_basis='CATEGORY_ONLY_NOT_SEMANTIC')
    return report


def verify_voc(manifest, report):
    checks = {}
    try:
        parsed = (VocReportWithHistory if manifest['task_version'] >= 3 else VocReport).model_validate(report)
        checks['schema'] = True
    except ValidationError:
        parsed = None
        checks['schema'] = False
    checks['source_snapshot_hash'] = digest(manifest['records']) == manifest['records_sha256']
    checks['method_hash'] = digest(manifest['method']['rules']) == manifest['method']['sha256']
    expected = {name:[] for name in CATEGORIES}
    unclassified = []
    for record in manifest['records']:
        matches = [category for category in CATEGORIES if any(term in record['body'] for term in manifest['method']['rules'][category])]
        (expected[matches[0]] if matches else unclassified).append(record['record_id'])
    if parsed:
        actual = {r.category:r for r in parsed.recommendations}
        needed = {category:ids for category,ids in expected.items() if ids}
        checks['counts_and_citations'] = (len(parsed.recommendations) == len(needed) and set(actual) == set(needed)
            and all(actual[category].count == len(ids) and actual[category].record_ids == ids
                    and actual[category].recommendation_id == f'REC-{category}' for category,ids in needed.items())
            and parsed.unclassified_ids == unclassified)
        checks['recommendation_templates'] = all(r.action == ACTIONS.get(r.category) for r in parsed.recommendations)
        checks['data_and_method_labels'] = (parsed.task_id == 'TASK-003' and parsed.data_mode == 'DEMO DATA'
            and parsed.model_mode == 'MOCK' and parsed.source_version == 'voc-demo-v1'
            and parsed.method_version == manifest['method']['method_id']
            and parsed.source_review_status == manifest['source_review_status'])
        checks['pii_patterns'] = all(not re.search(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b', r.action) for r in parsed.recommendations)
    else:
        checks.update(counts_and_citations=False,recommendation_templates=False,data_and_method_labels=False,pii_patterns=False)
    if manifest['task_version'] >= 3:
        previous = manifest.get('previous_report')
        previous_by_category = {item['category']:item['recommendation_id'] for item in previous['content']['recommendations']} if previous else {}
        checks['previous_snapshot_hash'] = previous is None or digest(previous['content'])==previous['sha256']
        checks['comparison_scope_and_reference'] = bool(parsed) and parsed.comparison_basis=='CATEGORY_ONLY_NOT_SEMANTIC' \
            and parsed.previous_artifact_id==(previous['artifact_id'] if previous else None) and parsed.history_as_of==manifest['history_as_of']
        checks['comparison_recomputed'] = bool(parsed) and all(
            item.previous_recommendation_id==previous_by_category.get(item.category)
            and item.comparison==('NO_PREVIOUS_REPORT' if previous is None else
                'SAME_CATEGORY' if item.category in previous_by_category else 'NEW_CATEGORY')
            for item in parsed.recommendations)
    return {'passed':all(checks.values()),'checks':checks,'semantic_usefulness':'NOT_HUMAN_EVALUATED'}
