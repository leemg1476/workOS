"""Machine-checked structure and canonical calculation table, not semantic truth."""
import json
from pydantic import BaseModel,ConfigDict,Field
from app.domain.hashing import digest
from sqlalchemy import text


class Section(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rule_id:str
    text:str=Field(min_length=1,max_length=10000)
    components:dict=Field(default_factory=dict)


class ReportClaim(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=1000)
    source_path:str=Field(min_length=1,max_length=250)
    value:str | int | float
    direction:str | None=None
    period_path:str | None=None
    period_value:str | None=None


class StructuredReport(BaseModel):
    model_config=ConfigDict(extra='forbid')
    introduction:str=Field(min_length=1,max_length=10000)
    sections:list[Section]=Field(max_length=20)
    claims:list[ReportClaim]=Field(default_factory=list,max_length=50)


def is_structured(knowledge):
    return any(e['kind'] in ('CHECKLIST','CALCULATION','CLAIM_CHECK') for e in knowledge['active'])


def render(raw,knowledge):
    draft=StructuredReport.model_validate_json(raw)
    rules={e['entry_id']:e for e in knowledge['active'] if e['kind']=='CHECKLIST'}
    ids=[s.rule_id for s in draft.sections]
    if len(ids)!=len(set(ids)) or set(ids)!=set(rules):raise ValueError('required report section missing, duplicated or unknown')
    parts=[draft.introduction]
    for section in draft.sections:
        parts.append('## '+rules[section.rule_id]['payload']['heading']+'\n\n'+section.text)
        spec=rules[section.rule_id].get('component_spec',{})
        if spec and section.components:
            shown=[]
            if 'formula' in spec and digest(section.components.get('formula'))==digest(spec['formula']):
                shown.append('산식: '+spec['display_formula'])
            if 'input_values' in spec and section.components.get('input_values')==spec['input_values']:
                shown.append('입력값: '+' · '.join(spec['labels'].get(p,p)+': '+str(v) for p,v in spec['input_values'].items()))
            if 'interpretation' in spec and section.components.get('interpretation')==spec['interpretation']:
                shown.append('해석: '+('품목 구성·수량을 고정하고 가격 변동률을 적용한 가상 예시이며 실제 가계의 지출 관측이 아닙니다.' if spec['interpretation']=='FIXED_BASKET_ESTIMATE' else '지정한 입력값을 적용한 계산 결과입니다. 입력의 범위와 가정을 함께 확인하세요.'))
            if shown:parts.append('\n\n'.join(shown))
    calculations=[e for e in knowledge['active'] if e['kind']=='CALCULATION']
    if calculations:
        parts+=['## 계산 근거 · Python Decimal\n\n|항목|계산 결과|\n|---|---:|\n'+'\n'.join(
            '|'+e['title'].replace('|','/')+'|'+e['value']+' '+e['unit'].replace('|','/')+'|' for e in calculations)]
    return '\n\n'.join(parts)


def reviewer_context(c,run_id):
    row=c.execute(text('SELECT manifest,sha256 FROM run_contexts WHERE run_id=:id'),{'id':run_id}).mappings().first()
    if not row or digest(row['manifest'])!=row['sha256']:raise ValueError('run context missing or changed')
    manifest=row['manifest'];sources=[]
    if 'contract' not in manifest:raise ValueError('review context is available for registered text reports only')
    for s in c.execute(text('SELECT * FROM report_source_snapshots WHERE run_id=:id ORDER BY captured_at,snapshot_id'),{'id':run_id}).mappings():
        if digest(s['text_content'])!=s['text_sha256']:raise ValueError('source snapshot hash changed')
        sources.append({'snapshot_id':str(s['snapshot_id']),'url':s['url'],'text_sha256':s['text_sha256'],
                        'text':s['text_content'],'scope':s['scope']})
    knowledge=manifest.get('task_memory',{}).get('manifest',{}).get('knowledge',{'active':[]})
    content=c.execute(text('SELECT content FROM artifacts WHERE run_id=:id ORDER BY created_at DESC LIMIT 1'),{'id':run_id}).scalar_one_or_none()
    return {'run_id':str(run_id),'source_text':manifest['contract']['source_text'],'source_sha256':manifest['source_sha256'],
            'source_snapshots':sources,'source_readbacks':[(s.get('research') or {}).get('source_snapshots',[]) for s in (content or {}).get('steps',[])],
            'knowledge':knowledge,'report':{'steps':[{'step_id':s['step_id'],'text':s['text']} for s in (content or {}).get('steps',[])]},
            'review_policy':'AI_SCORE_IS_ADVISORY; ABSENT_EVIDENCE_IS_UNKNOWN_NOT_FALSE; QUOTE_ERROR_CLAIMS_AND_COMPARE_ACTUAL_SOURCE; NO_AUTO_APPLY'}
