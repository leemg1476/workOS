"""Deterministic selection and compilation of pinned procedures per workflow step."""
from copy import deepcopy
from decimal import Decimal
from sqlalchemy import text
from app.domain.hashing import digest,canonical
from app.domain.experience import ProceduralRule,matches,pointer
from app.work.procedural_knowledge import source_object
from app.work.service import uid,Conflict

VERSION='experience-compiler-v1'


def display_formula(expression,fields,depth=0):
    if depth>8:raise ValueError('formula too deep')
    if 'path' in expression:return fields.get(expression['path'],{}).get('meaning',expression['path'])
    if 'value' in expression:return str(expression['value'])
    symbol={'add':' + ','multiply':' × ','subtract':' − ','divide':' ÷ '}[expression['op']]
    return '('+symbol.join(display_formula(a,fields,depth+1) for a in expression['args'])+')'


def compile_step(memory,contract,step_id,binding,*,completed_steps=(),environment='LOCAL',failure_type=None):
    from app.work.knowledge import calculate
    from app.work.task_memory import prompt_view
    view=deepcopy(prompt_view(memory))
    execution=memory.get('execution_context',{})
    environment=execution.get('environment',environment);failure_type=execution.get('failure_type',failure_type)
    source=source_object(contract['source_text']);role=(binding or {}).get('step_roles',{}).get(step_id,'WRITE')
    final=contract['plan']['steps'][-1]['id']==step_id
    context={'source':source,'environment':environment,'failure_type':failure_type,'step_id':step_id,'role':role}
    trace={'compiler_version':VERSION,'step_id':step_id,'role':role,'candidate_rule_ids':[],
           'selected_rule_ids':[],'excluded_reasons':[],'target_step_ids':[step_id],'compiled_actions':[],
           'preconditions':[],'source_sha256':digest(contract['source_text']),
           'execution_context':{'environment':environment,'failure_type':failure_type},
           'quality_delta':None,'quality_delta_reason':'REQUIRES_FIXED_PAIRED_EVALUATION'}
    active=[]
    for item in memory.get('knowledge',{}).get('active',[]):
        if item['kind']!='RULE':
            allowed=(item['kind']=='CHECKLIST' and final or
                     item['kind']=='CALCULATION' and role in ('CALCULATE','WRITE') or
                     item['kind']=='SOURCE_EXCERPT' and role in ('RESEARCH','INTERPRET','WRITE','REVIEW'))
            if allowed:active.append(deepcopy(item))
            else:trace['excluded_reasons'].append({'entry_id':item['entry_id'],'reason':'LEGACY_ADVICE_REQUIRES_NORMALIZATION' if item['kind']=='LESSON' else 'LEGACY_MEMORY_NOT_RELEVANT_TO_STEP'})
            continue
        rule=ProceduralRule.model_validate(item['payload']);trace['candidate_rule_ids'].append(item['entry_id'])
        reason=None
        if step_id not in rule.target_steps and role not in rule.target_roles:reason='TARGET_STEP_MISMATCH'
        elif not all(matches(p,context) for p in rule.when):reason='TRIGGER_NOT_SATISFIED'
        elif any(matches(p,context) for p in rule.unless):reason='EXCLUSION_CONDITION'
        elif rule.scope.kind=='FAILURE_ENV' and (rule.scope.failure_type!=failure_type or rule.scope.environment!=environment):reason='FAILURE_ENVIRONMENT_MISMATCH'
        if reason:
            trace['excluded_reasons'].append({'entry_id':item['entry_id'],'reason':reason});continue
        trace['selected_rule_ids'].append(item['entry_id'])
        for action in rule.actions:
            action_id=item['entry_id']+'/'+action.action_id
            row={'entry_id':action_id,'parent_rule_id':item['entry_id'],'title':getattr(action,'label',item['title']),
                 'rule_sha256':item['sha256'],'action':action.model_dump(),'verification':rule.verification.model_dump()}
            if action.kind=='REQUIRE_SECTION':
                row.update(kind='CHECKLIST',payload={'heading':action.heading,'instruction':action.instruction},
                           required_components=action.required_components)
                spec={}
                if 'formula' in action.required_components:
                    spec.update(formula=action.formula,display_formula=display_formula(action.formula,(binding or {}).get('input_fields',{})))
                if 'input_values' in action.required_components:
                    spec.update(input_values={p:pointer(source,p) for p in action.input_paths},
                                labels={p:((binding or {}).get('input_fields',{}).get(p,{}).get('meaning',p)+' ['+(binding or {}).get('input_fields',{}).get(p,{}).get('unit','')+']') for p in action.input_paths})
                if 'interpretation' in action.required_components:spec['interpretation']=action.interpretation_code
                row['component_spec']=spec
                active.append(row)
            elif action.kind=='CALCULATE':
                number=calculate(action.expression,source).quantize(Decimal(1).scaleb(-action.decimal_places))
                row.update(kind='CALCULATION',value=str(number),unit=action.unit,expression=action.expression,
                           scope='DETERMINISTIC_DECIMAL_RESULT')
                active.append(row)
            elif action.kind=='PRECONDITION':
                passed=all(matches(p,context) for p in action.conditions) and set(action.required_steps)<=set(completed_steps)
                trace['preconditions'].append({'entry_id':action_id,'passed':passed,'required_steps':action.required_steps})
            elif action.kind=='CHECK_CLAIMS':
                row.update(kind='CLAIM_CHECK')
                active.append(row)
            row['sha256']=digest({k:v for k,v in row.items() if k!='sha256'})
            trace['compiled_actions'].append(deepcopy(row))
    view['knowledge']={'active':active,'excluded':trace['excluded_reasons'],'sha256':digest(active),'version':VERSION}
    view['selected_procedures']=[{'entry_id':i['entry_id'],'rule':i['payload']} for i in memory.get('knowledge',{}).get('active',[]) if i['entry_id'] in trace['selected_rule_ids']]
    if role not in ('WRITE','INTERPRET'):
        view['accepted_results']=[];view['draft_results']=[]
    if role not in ('RESEARCH','INTERPRET','WRITE','REVIEW'):
        view['source_library']=[]
    if role not in ('RESEARCH','REVIEW'):
        view['failures']=[f for f in view.get('failures',[]) if role=='WRITE' and f.get('status')=='VERIFICATION_FAILED']
    trace['prompt_memory_sha256']=digest(view)
    return view,trace


def persist_trace(kernel,c,run_id,step_id,phase,snapshot):
    sha=digest(snapshot)
    old=c.execute(text('SELECT snapshot_sha256 FROM memory_application_traces WHERE run_id=:run AND step_id=:step AND phase=:phase'),
                  {'run':run_id,'step':step_id,'phase':phase}).scalar_one_or_none()
    if old:
        if old!=sha:raise Conflict('memory application trace changed')
        return
    c.execute(text('''INSERT INTO memory_application_traces(trace_id,run_id,step_id,phase,snapshot,snapshot_sha256)
        VALUES (:id,:run,:step,:phase,CAST(:snapshot AS jsonb),:sha)'''),
        {'id':uid(),'run':run_id,'step':step_id,'phase':phase,'snapshot':canonical(snapshot),'sha':sha})
    kernel._event(c,'memory.'+phase.lower(),str(run_id),{'step_id':step_id,'trace_sha256':sha,'compiler_version':VERSION})


def verify_output(raw,knowledge,source):
    """Check structured domain assertions independently; prose semantics stay unproven."""
    import json,re
    from app.evaluation.report_quality import StructuredReport
    from app.work.knowledge import calculate
    results=[]
    try:draft=StructuredReport.model_validate_json(raw)
    except (ValueError,TypeError):
        return [{'entry_id':i['entry_id'],'parent_rule_id':i['parent_rule_id'],'passed':False,'reason':'INVALID_STRUCTURED_OUTPUT',
            'blocking':i['verification']['severity']=='BLOCK'} for i in knowledge['active'] if i.get('parent_rule_id')]
    sections={s.rule_id:s for s in draft.sections};obj=source_object(source)
    for item in knowledge['active']:
        if not item.get('parent_rule_id'):continue
        action=item['action'];checks={};reason=None
        if item['kind']=='CHECKLIST':
            section=sections.get(item['entry_id']);checks['section_present']=section is not None
            components=section.components if section else {}
            for component in action['required_components']:
                if component=='formula':
                    checks['formula']=digest(components.get('formula'))==digest(action['formula'])
                    if checks['formula']:
                        try:calculate(components['formula'],obj)
                        except (ValueError,TypeError,KeyError,ArithmeticError):checks['formula']=False
                elif component=='input_values':
                    expected={p:pointer(obj,p) for p in action['input_paths']}
                    checks['input_values']=digest(components.get('input_values'))==digest(expected)
                elif component=='interpretation':checks['interpretation']=components.get('interpretation')==action['interpretation_code']
        elif item['kind']=='CALCULATION':checks['canonical_calculation']=True  # Recomputed by compile_step and rendered by the server.
        elif item['kind']=='CLAIM_CHECK':
            body=draft.introduction+'\n'+'\n'.join(s.text for s in draft.sections)
            covered=set();claim_checks=[]
            for claim in draft.claims:
                ok=claim.text in body and claim.source_path in action['source_paths']
                try:
                    actual=pointer(obj,claim.source_path)
                    if type(actual) in (int,float):
                        ok &= Decimal(str(actual))==Decimal(str(claim.value))
                        numbers=re.findall(r'-?\d+(?:\.\d+)?',claim.text.replace(',',''))
                        ok &= any(abs(Decimal(n))==abs(Decimal(str(actual))) for n in numbers)
                    else:ok &= str(actual)==str(claim.value) and str(actual) in claim.text
                    if claim.direction:
                        expected='DOWN' if Decimal(str(actual))<0 else 'UP' if Decimal(str(actual))>0 else 'FLAT'
                        words={'DOWN':('하락','감소','낮아','내렸'),'UP':('상승','증가','높아','올랐'),'FLAT':('동일','변동 없')}
                        opposite={'DOWN':words['UP'],'UP':words['DOWN'],'FLAT':words['UP']+words['DOWN']}
                        ok &= claim.direction==expected and any(w in claim.text for w in words[expected]) and not any(w in claim.text for w in opposite[expected])
                    if action['require_period']:
                        period=pointer(obj,claim.period_path) if claim.period_path else None
                        ok &= claim.period_path==action.get('period_path') and period is not None and str(period)==claim.period_value and str(period) in claim.text
                except (ValueError,TypeError,KeyError,IndexError,ArithmeticError):ok=False
                claim_checks.append(bool(ok))
                if ok:covered.add(claim.source_path)
            checks={'claims_supported':bool(claim_checks) and all(claim_checks),'required_paths_covered':set(action['source_paths'])<=covered}
        if checks:
            results.append({'entry_id':item['entry_id'],'parent_rule_id':item['parent_rule_id'],'checks':checks,
                'passed':all(checks.values()),'scope':'CONFIGURED_DOMAIN_ASSERTIONS_NOT_FULL_PROSE_ENTAILMENT',
                'semantic_review':'PENDING_HUMAN' if item['verification']['semantic_review']=='HUMAN_REQUIRED' else 'DOMAIN_ASSERTIONS_ONLY',
                'blocking':item['verification']['severity']=='BLOCK'})
    return results
