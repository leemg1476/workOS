"""Frozen domain assertions authored independently of candidate rules."""
from pydantic import Field,model_validator
from app.domain.experience import Contract,pointer
from app.domain.hashing import digest
from app.work.procedural_knowledge import source_object


class DomainAssertion(Contract):
    suite_key:str=Field(min_length=1,max_length=100)
    kind:str=Field(pattern=r'^(FORMULA_EXPLANATION|SUPPORTED_CLAIMS)$')
    formula:dict | None=None
    input_paths:list[str]=Field(default_factory=list,max_length=30)
    interpretation_code:str=Field(default='ARITHMETIC_ON_PROVIDED_INPUT',pattern=r'^(ARITHMETIC_ON_PROVIDED_INPUT|FIXED_BASKET_ESTIMATE)$')
    source_paths:list[str]=Field(default_factory=list,max_length=30)
    require_period:bool=True
    period_path:str | None=None

    @model_validator(mode='after')
    def has_oracle(self):
        if self.kind=='FORMULA_EXPLANATION' and (not self.formula or not self.input_paths):
            raise ValueError('formula oracle requires an AST and input paths')
        if self.kind=='SUPPORTED_CLAIMS' and not self.source_paths:
            raise ValueError('claim oracle requires source paths')
        if self.kind=='SUPPORTED_CLAIMS' and self.require_period and not self.period_path:raise ValueError('claim oracle needs the expected period field')
        return self


def evaluate_assertions(part,source,assertions):
    from app.evaluation.report_quality import StructuredReport
    from app.work.memory_compiler import verify_output
    raw=part.get('structured_report') or part.get('text','');results=[]
    for value in assertions:
        assertion=DomainAssertion.model_validate(value)
        try:
            draft=StructuredReport.model_validate_json(raw)
            if assertion.kind=='FORMULA_EXPLANATION':
                # The semantic representation must carry a source-valid formula, actual
                # inputs and the declared interpretation. A nonempty section is insufficient.
                expected={p:pointer(source_object(source),p) for p in assertion.input_paths}
                from app.work.knowledge import calculate
                calculate(assertion.formula,source_object(source))
                passed=any(digest(s.components.get('formula'))==digest(assertion.formula) and
                    digest(s.components.get('input_values'))==digest(expected) and
                    s.components.get('interpretation')==assertion.interpretation_code for s in draft.sections)
            else:
                fake={'entry_id':'frozen-claim-oracle','parent_rule_id':'frozen-suite','kind':'CLAIM_CHECK',
                      'action':{'source_paths':assertion.source_paths,'require_period':assertion.require_period,'period_path':assertion.period_path},
                      'verification':{'semantic_review':'DOMAIN_ASSERTIONS','severity':'BLOCK'}}
                verdict=verify_output(raw,{'active':[fake]},source)
                passed=bool(verdict) and all(v['passed'] for v in verdict)
        except (ValueError,TypeError,KeyError,IndexError,ArithmeticError):passed=False
        results.append({'suite_key':assertion.suite_key,'kind':assertion.kind,'passed':passed,
            'scope':'STRUCTURED_DOMAIN_MEANING_NOT_GENERAL_PROSE_SEMANTICS','oracle_sha256':digest(value)})
    return results
