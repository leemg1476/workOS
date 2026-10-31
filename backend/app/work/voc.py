"""Deterministic synthetic VOC workload and a fixed, labeled demo evaluation suite."""
import hashlib
import re
from pydantic import ValidationError
from app.domain.contracts import VocReport, VocReportWithHistory, VocReportWithSources
from app.work.market import digest

CATEGORIES = ('billing','delivery','support')
ACTIONS = {'billing':'결제·환불 안내의 누락 구간을 점검한다.',
           'delivery':'배송 상태와 도착 안내를 점검한다.',
           'support':'고객 문의와 상담 응대 경로를 점검한다.'}
DOCUMENTS = {'billing':'DOC-VOC-BILLING@1','delivery':'DOC-VOC-DELIVERY@1','support':'DOC-VOC-SUPPORT@1'}
DOCUMENT_QUERY = ('SELECT document_id,version,title,body,sha256 FROM source_documents '
                  'WHERE document_id IN (:billing,:delivery,:support) AND version=:version ORDER BY document_id,version')
DOCUMENT_PARAMETERS = {'billing':'DOC-VOC-BILLING','delivery':'DOC-VOC-DELIVERY','support':'DOC-VOC-SUPPORT','version':1}
RECORD_QUERY = "SELECT record_id,source_version,body FROM voc_records WHERE source_version=:version ORDER BY record_id"
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
    if manifest['task_version'] >= 4:
        for item in report['recommendations']:
            item['document_citations'] = [DOCUMENTS[item['category']]]
        report['retrieval_mode'] = manifest['retrieval']['mode']
    return report


def verify_voc(manifest, report):
    checks = {}
    try:
        contract = VocReportWithSources if manifest['task_version'] >= 4 else VocReportWithHistory if manifest['task_version'] >= 3 else VocReport
        parsed = contract.model_validate(report)
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
    if manifest['task_version'] >= 4:
        documents = manifest['documents']
        present = {d['id'] for d in documents if hashlib.sha256(d['body'].encode('utf-8')).hexdigest()==d['sha256']}
        checks['retrieval_snapshot_hash'] = digest(documents)==manifest['retrieval_result_sha256']
        checks['document_versions_and_hashes'] = present==set(DOCUMENTS.values()) and documents==manifest['authoritative_documents']
        checks['document_citations'] = bool(parsed) and parsed.retrieval_mode==manifest['retrieval']['mode'] and all(
            item.document_citations==[DOCUMENTS[item.category]] and set(item.document_citations).issubset(present)
            for item in parsed.recommendations)
    return {'passed':all(checks.values()),'checks':checks,'semantic_usefulness':'NOT_HUMAN_EVALUATED'}
