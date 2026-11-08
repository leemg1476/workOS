"""Typed procedural knowledge. Data facts and reusable methods have different scopes."""
from typing import Literal, Union, Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class InputField(Contract):
    type: Literal['number', 'string', 'boolean', 'object', 'array']
    unit: str = Field(min_length=1, max_length=60)
    meaning: str = Field(min_length=1, max_length=200)


class ProcedureBinding(Contract):
    family: str = Field(min_length=1, max_length=100)
    contract_key: str = Field(min_length=1, max_length=100)
    step_roles: dict[str, Literal['RESEARCH', 'CALCULATE', 'INTERPRET', 'WRITE', 'REVIEW']]
    input_fields: dict[str, InputField] = Field(default_factory=dict, max_length=100)


class Predicate(Contract):
    path: str = Field(pattern=r'^/', max_length=250)
    op: Literal['EXISTS', 'EQ', 'NE', 'GTE', 'LTE']
    value: Union[str, int, float, bool, None] = None


class RuleScope(Contract):
    kind: Literal['EXACT_INPUT', 'TASK_CONTRACT', 'INPUT_SCHEMA', 'TASK_FAMILY', 'FAILURE_ENV']
    binding_sha256: str = Field(min_length=64, max_length=64)
    input_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    failure_type: str | None = Field(default=None, max_length=100)
    environment: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def complete(self):
        if self.kind == 'EXACT_INPUT' and not self.input_sha256:
            raise ValueError('exact input rules require an input hash')
        if self.kind == 'FAILURE_ENV' and not (self.failure_type and self.environment):
            raise ValueError('failure rules require failure type and environment')
        return self


class RequireSection(Contract):
    kind: Literal['REQUIRE_SECTION']
    action_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,59}$')
    heading: str = Field(min_length=1, max_length=150)
    instruction: str = Field(min_length=1, max_length=2000)
    required_components: list[Literal['formula', 'input_values', 'interpretation']] = Field(default_factory=list, max_length=3)
    formula: dict | None = None
    input_paths: list[str] = Field(default_factory=list, max_length=30)
    interpretation_code: Literal['ARITHMETIC_ON_PROVIDED_INPUT', 'FIXED_BASKET_ESTIMATE'] = 'ARITHMETIC_ON_PROVIDED_INPUT'

    @model_validator(mode='after')
    def verifiable(self):
        if len(set(self.required_components)) != len(self.required_components):
            raise ValueError('duplicate required component')
        if 'formula' in self.required_components and self.formula is None:
            raise ValueError('formula explanation needs a checkable arithmetic AST')
        if 'input_values' in self.required_components and not self.input_paths:
            raise ValueError('input explanation needs source paths')
        return self


class CalculateAction(Contract):
    kind: Literal['CALCULATE']
    action_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,59}$')
    label: str = Field(min_length=1, max_length=150)
    expression: dict
    unit: str = Field(min_length=1, max_length=30)
    decimal_places: int = Field(ge=0, le=8)


class PreconditionAction(Contract):
    kind: Literal['PRECONDITION']
    action_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,59}$')
    conditions: list[Predicate] = Field(min_length=1, max_length=20)
    required_steps: list[str] = Field(default_factory=list, max_length=12)


class ClaimAction(Contract):
    kind: Literal['CHECK_CLAIMS']
    action_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,59}$')
    source_paths: list[str] = Field(min_length=1, max_length=30)
    require_period: bool = True
    period_path: str | None = None

    @model_validator(mode='after')
    def period_is_declared(self):
        if self.require_period and not self.period_path:raise ValueError('claim checks require an explicit period field')
        return self


Action = Annotated[Union[RequireSection, CalculateAction, PreconditionAction, ClaimAction], Field(discriminator='kind')]


class RuleVerification(Contract):
    suite_key: str = Field(min_length=1, max_length=100)
    severity: Literal['BLOCK', 'ADVISORY'] = 'BLOCK'
    semantic_review: Literal['DOMAIN_ASSERTIONS', 'HUMAN_REQUIRED']


class MethodParameter(Contract):
    value: float = Field(allow_inf_nan=False,strict=True)
    meaning: str = Field(min_length=1,max_length=200)
    rationale: str = Field(min_length=1,max_length=1000)


class ProceduralRule(Contract):
    version: Literal['procedural-rule-v1'] = 'procedural-rule-v1'
    rule_key: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9._-]{0,119}$')
    failure_mode: Literal['FACT_ERROR', 'PROCEDURE_ERROR', 'OUTPUT_OMISSION', 'REASONING_ERROR', 'TECHNICAL_FAILURE']
    root_cause: str = Field(min_length=1, max_length=2000)
    scope: RuleScope
    target_steps: list[str] = Field(default_factory=list, max_length=12)
    target_roles: list[Literal['RESEARCH', 'CALCULATE', 'INTERPRET', 'WRITE', 'REVIEW']] = Field(default_factory=list, max_length=5)
    when: list[Predicate] = Field(default_factory=list, max_length=20)
    unless: list[Predicate] = Field(default_factory=list, max_length=20)
    actions: list[Action] = Field(min_length=1, max_length=8)
    verification: RuleVerification
    policy_slot: str = Field(min_length=1, max_length=120)
    policy_value: str = Field(min_length=1, max_length=120)
    method_parameters: dict[str,MethodParameter] = Field(default_factory=dict,max_length=20)

    @model_validator(mode='after')
    def targets_and_actions(self):
        if not self.target_steps and not self.target_roles:
            raise ValueError('a rule needs explicit step IDs or roles')
        if len({a.action_id for a in self.actions}) != len(self.actions):
            raise ValueError('action IDs must be unique')
        return self


class BindingRequest(Contract):
    binding: ProcedureBinding
    idempotency_key: str = Field(min_length=1, max_length=150)


class KnowledgeRelation(Contract):
    target_entry_id: str
    kind: Literal['DERIVED_FROM', 'DUPLICATE_OF', 'CONFLICTS_WITH', 'SUPERSEDES', 'ENRICHES']
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=150)


def pointer(value, path):
    if not path.startswith('/'):
        raise ValueError('JSON pointer required')
    for key in path[1:].split('/'):
        key = key.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def matches(predicate, context):
    try:
        actual = pointer(context, predicate.path)
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if predicate.op == 'EXISTS':
        return True
    if predicate.op == 'EQ':
        return type(actual) is type(predicate.value) and actual == predicate.value
    if predicate.op == 'NE':
        return not (type(actual) is type(predicate.value) and actual == predicate.value)
    if type(actual) not in (int, float) or type(predicate.value) not in (int, float):
        return False
    return actual >= predicate.value if predicate.op == 'GTE' else actual <= predicate.value
