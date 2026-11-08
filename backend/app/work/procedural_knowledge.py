"""Scope, provenance and lifecycle rules for reusable procedures; no model authority."""
import json
import math
from decimal import Decimal
from uuid import UUID
from sqlalchemy import text
from app.domain.hashing import digest, canonical
from app.domain.experience import ProcedureBinding, ProceduralRule, pointer
from app.work.service import Kernel, Conflict, NotFound, one, uid


def source_object(source):
    try:
        return json.loads(source)
    except (json.JSONDecodeError, TypeError):
        return {'text': source}


def validate_inputs(binding, source):
    obj = source_object(source)
    for path, field in binding.input_fields.items():
        value = pointer(obj, path)
        valid = {'number': type(value) is int or type(value) is float and math.isfinite(value), 'string': isinstance(value, str),
                 'boolean': type(value) is bool, 'object': isinstance(value, dict), 'array': isinstance(value, list)}[field.type]
        if not valid:
            raise ValueError('input field type does not match procedural contract: ' + path)


def binding_for(c, task_id, version):
    row = one(c, 'SELECT * FROM task_procedure_bindings WHERE task_id=:task AND task_version=:v', {'task': task_id, 'v': version})
    if row and digest(row['binding']) != row['binding_sha256']:
        raise Conflict('procedure binding hash mismatch')
    return row


def carry_forward_binding(kernel,c,task_id,old_version,new_version):
    """A user-applied method may inherit only an unchanged field/step contract."""
    from app.work.knowledge import source_text
    original=binding_for(c,task_id,old_version)
    if not original or binding_for(c,task_id,new_version):return
    old=one(c,'SELECT * FROM task_definitions WHERE task_id=:task AND version=:v',{'task':task_id,'v':old_version})
    new=one(c,'SELECT * FROM task_definitions WHERE task_id=:task AND version=:v',{'task':task_id,'v':new_version})
    binding=ProcedureBinding.model_validate(original['binding'])
    compatible=old['input_schema']==new['input_schema'] and old['objective']==new['objective'] and old['output_schema'].get('execution_kind')==new['output_schema'].get('execution_kind')
    compatible &= set(binding.step_roles)=={s['id'] for s in new['output_schema'].get('plan',{}).get('steps',[])}
    try:validate_inputs(binding,source_text(c,task_id,new_version))
    except (ValueError,KeyError,IndexError,TypeError):compatible=False
    if not compatible:
        kernel._event(c,'knowledge.binding_review_required',task_id,{'from_version':old_version,'to_version':new_version,'reason':'FIELD_OR_STEP_CONTRACT_CHANGED'})
        return
    c.execute(text('''INSERT INTO task_procedure_bindings(task_id,task_version,binding,binding_sha256)
        VALUES (:task,:v,CAST(:b AS jsonb),:sha)'''),{'task':task_id,'v':new_version,'b':canonical(original['binding']),'sha':original['binding_sha256']})
    kernel._event(c,'knowledge.binding_inherited',task_id,{'from_version':old_version,'to_version':new_version,'binding_sha256':original['binding_sha256']})


def bind(engine, task_id, request):
    from app.work.knowledge import source_text
    kernel = Kernel(engine)
    payload = request.binding.model_dump()
    with engine.begin() as c:
        kernel._lock(c)
        task = one(c, 'SELECT * FROM task_definitions WHERE task_id=:id AND active', {'id': task_id})
        if not task:
            raise NotFound('active task not found')
        plan = task['output_schema'].get('plan', {})
        if set(payload['step_roles']) != {s['id'] for s in plan.get('steps', [])}:
            raise ValueError('assign a role to every current plan step, with no unknown steps')
        validate_inputs(request.binding, source_text(c, task_id, task['version']))
        old = binding_for(c, task_id, task['version'])
        if old:
            if old['binding'] != payload:
                raise Conflict('binding is immutable; use a new task contract version')
            return old
        sha = digest(payload)
        c.execute(text('''INSERT INTO task_procedure_bindings(task_id,task_version,binding,binding_sha256)
            VALUES (:task,:v,CAST(:b AS jsonb),:sha)'''), {'task': task_id, 'v': task['version'], 'b': canonical(payload), 'sha': sha})
        kernel._event(c, 'knowledge.binding_created', task_id, {'task_version': task['version'], 'binding_sha256': sha},
                      actor='user', key='knowledge.bind:' + request.idempotency_key)
        return binding_for(c, task_id, task['version'])


def validate_rule(c, task_id, version, payload, source):
    rule = ProceduralRule.model_validate(payload)
    bound = binding_for(c, task_id, version)
    if not bound or rule.scope.binding_sha256 != bound['binding_sha256']:
        raise Conflict('rule needs the current explicit procedure binding')
    binding = ProcedureBinding.model_validate(bound['binding'])
    if not set(rule.target_steps) <= set(binding.step_roles):
        raise ValueError('rule target step does not exist')
    validate_inputs(binding, source)
    from app.work.knowledge import calculate
    def check_formula(expression):
        calculate(expression,source_object(source))
        def visit(node):
            if 'path' in node:
                if node['path'] not in binding.input_fields:raise ValueError('formula field needs an explicit meaning/unit binding')
            elif 'value' in node and rule.scope.kind!='EXACT_INPUT':
                number=Decimal(str(node['value']))
                conversions={Decimal(x) for x in ('-1','0','0.01','1','12','100')}
                parameters={Decimal(str(p.value)) for p in rule.method_parameters.values()}
                if number not in conversions|parameters:raise ValueError('portable formulas must use current source fields; declare method parameters separately from source facts')
            for child in node.get('args',[]):visit(child)
        visit(expression)
    for action in rule.actions:
        if action.kind == 'CALCULATE':
            check_formula(action.expression)
        elif action.kind == 'REQUIRE_SECTION':
            if action.formula is not None:
                check_formula(action.formula)
            for path in action.input_paths:
                if path not in binding.input_fields:raise ValueError('explanation field needs a meaning/unit binding')
                pointer(source_object(source), path)
        elif action.kind == 'PRECONDITION':
            if not set(action.required_steps) <= set(binding.step_roles):
                raise ValueError('required workflow step does not exist')
        elif action.kind == 'CHECK_CLAIMS':
            for path in action.source_paths+([action.period_path] if action.period_path else []):
                if path not in binding.input_fields:raise ValueError('claim fields must be bound with their meaning and unit')
                pointer(source_object(source), path)
    return rule


def applicable(c, entry, task_id, version, source):
    rule = ProceduralRule.model_validate(entry['payload'])
    original = binding_for(c, entry['task_id'], entry['task_version'])
    current = binding_for(c, task_id, version)
    if not original or not current or original['binding_sha256'] != rule.scope.binding_sha256:
        return False, 'PROCEDURE_BINDING_MISSING_OR_CHANGED'
    old, new = original['binding'], current['binding']
    kind = rule.scope.kind
    if kind != 'TASK_FAMILY' and entry['task_id'] != task_id:
        return False, 'OTHER_TASK_NOT_FAMILY_SCOPED'
    if kind == 'EXACT_INPUT' and (entry['task_version'] != version or rule.scope.input_sha256 != digest(source)):
        return False, 'EXACT_INPUT_OR_VERSION_CHANGED'
    if kind in ('TASK_CONTRACT', 'EXACT_INPUT', 'FAILURE_ENV') and old != new:
        return False, 'PROCEDURE_CONTRACT_CHANGED'
    if kind in ('INPUT_SCHEMA', 'TASK_FAMILY') and old['input_fields'] != new['input_fields']:
        return False, 'INPUT_FIELD_SEMANTICS_CHANGED'
    if kind == 'TASK_FAMILY' and (old['family'] != new['family'] or old['contract_key'] != new['contract_key']):
        return False, 'TASK_FAMILY_CONTRACT_CHANGED'
    if not set(rule.target_steps) <= set(new['step_roles']):
        return False, 'TARGET_STEP_REMOVED'
    try:
        validate_inputs(ProcedureBinding.model_validate(new), source)
    except (ValueError, KeyError, IndexError, TypeError):
        return False, 'INPUT_SCHEMA_NOT_SATISFIED'
    return True, None


def candidates(c, task_id):
    """Only structured procedures can cross task boundaries via explicit family scope."""
    from app.work.knowledge import entries
    rows = entries(c, task_id)
    others = c.execute(text("SELECT DISTINCT task_id FROM task_knowledge WHERE kind='RULE' AND task_id<>:task"), {'task': task_id}).scalars()
    for other in others:
        rows.extend(r for r in entries(c, other) if r['kind'] == 'RULE' and r['payload']['scope']['kind'] == 'TASK_FAMILY')
    return rows


def relation(engine, entry_id, request):
    UUID(entry_id); UUID(request.target_entry_id)
    kernel = Kernel(engine)
    with engine.begin() as c:
        kernel._lock(c)
        if entry_id == request.target_entry_id:
            raise Conflict('self relation is not allowed')
        pair = c.execute(text('SELECT * FROM task_knowledge WHERE entry_id IN (CAST(:a AS uuid),CAST(:b AS uuid))'),
                         {'a': entry_id, 'b': request.target_entry_id}).mappings().all()
        if len(pair) != 2 or pair[0]['task_id'] != pair[1]['task_id']:
            raise Conflict('relations require two entries owned by the same task')
        body = {'entry_id': entry_id, **request.model_dump()}
        key = 'knowledge.relation:' + request.idempotency_key
        previous = one(c, 'SELECT payload FROM work_events WHERE idempotency_key=:key', {'key': key})
        if previous:
            if previous['payload'] != body:
                raise Conflict('relation idempotency conflict')
            return False
        cyclic = c.execute(text('''WITH RECURSIVE ancestors(id) AS (
            SELECT target_entry_id FROM knowledge_relations WHERE entry_id=CAST(:target AS uuid) AND kind=:kind
            UNION SELECT r.target_entry_id FROM knowledge_relations r JOIN ancestors a ON r.entry_id=a.id WHERE r.kind=:kind)
            SELECT 1 FROM ancestors WHERE id=CAST(:entry AS uuid) LIMIT 1'''),
            {'target': request.target_entry_id, 'entry': entry_id, 'kind': request.kind}).first()
        if cyclic and request.kind != 'CONFLICTS_WITH':
            raise Conflict('cyclic knowledge relation')
        # An active pair must be resolved before recording a newly discovered conflict.
        if request.kind == 'CONFLICTS_WITH':
            from app.work.knowledge import entries
            applied = {str(e['entry_id']) for e in entries(c, pair[0]['task_id']) if e['decision'] == 'APPLY'}
            if {entry_id, request.target_entry_id} <= applied:
                raise Conflict('retire one conflicting rule before confirming the relation')
        c.execute(text('''INSERT INTO knowledge_relations(relation_id,entry_id,target_entry_id,kind,note)
            VALUES (:id,CAST(:a AS uuid),CAST(:b AS uuid),:kind,:note) ON CONFLICT(entry_id,target_entry_id,kind) DO NOTHING'''),
            {'id': uid(), 'a': entry_id, 'b': request.target_entry_id, 'kind': request.kind, 'note': request.note})
        kernel._event(c, 'knowledge.relation', entry_id, body, actor='user', key=key)
        return True


def activation_conflicts(c, entry, active, replacing=()):
    own = str(entry['entry_id']); replacing = set(replacing)
    relations = c.execute(text('''SELECT * FROM knowledge_relations
        WHERE entry_id=CAST(:id AS uuid) OR target_entry_id=CAST(:id AS uuid)'''), {'id': own}).mappings().all()
    active_ids = {str(e['entry_id']) for e in active if str(e['entry_id']) not in replacing and str(e['entry_id']) != own}
    for row in relations:
        other = str(row['target_entry_id'] if str(row['entry_id']) == own else row['entry_id'])
        if other in active_ids and row['kind'] in ('CONFLICTS_WITH', 'SUPERSEDES', 'DUPLICATE_OF'):
            raise Conflict('active knowledge relation requires an explicit replacement or retirement: ' + other)
    if entry['kind'] == 'RULE':
        rule = ProceduralRule.model_validate(entry['payload'])
        for old in active:
            if old['kind'] != 'RULE' or str(old['entry_id']) not in active_ids:
                continue
            other = ProceduralRule.model_validate(old['payload'])
            bound=binding_for(c,entry['task_id'],entry['task_version'])
            roles=(bound or {}).get('binding',{}).get('step_roles',{})
            targets=lambda r:set(r.target_steps)|{s for s,role in roles.items() if role in r.target_roles}
            overlapping=bool(targets(rule)&targets(other))
            if overlapping and rule.policy_slot == other.policy_slot:
                if rule.policy_value != other.policy_value:
                    raise Conflict('contradicting active policy slot: ' + rule.policy_slot)
                if digest([a.model_dump() for a in rule.actions]) == digest([a.model_dump() for a in other.actions]):
                    raise Conflict('equivalent active procedure; link supporting evidence instead of activating a duplicate')
