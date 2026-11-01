from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class WorkStatus(str, Enum):
    CREATED = 'CREATED'
    READY = 'READY'
    RUNNING = 'RUNNING'
    SUBMITTED = 'SUBMITTED'
    VERIFYING = 'VERIFYING'
    ACCEPTED = 'ACCEPTED'
    COMMITTED = 'COMMITTED'
    REWORK_REQUIRED = 'REWORK_REQUIRED'
    STALE = 'STALE'
    BLOCKED = 'BLOCKED'
    CANCELLED = 'CANCELLED'
    WAITING_APPROVAL = 'WAITING_APPROVAL'


TRANSITIONS = {
    WorkStatus.CREATED: {WorkStatus.READY, WorkStatus.BLOCKED, WorkStatus.WAITING_APPROVAL, WorkStatus.CANCELLED},
    WorkStatus.READY: {WorkStatus.RUNNING, WorkStatus.BLOCKED, WorkStatus.CANCELLED},
    WorkStatus.RUNNING: {WorkStatus.SUBMITTED, WorkStatus.REWORK_REQUIRED, WorkStatus.CANCELLED},
    WorkStatus.SUBMITTED: {WorkStatus.VERIFYING, WorkStatus.REWORK_REQUIRED},
    WorkStatus.VERIFYING: {WorkStatus.ACCEPTED, WorkStatus.REWORK_REQUIRED, WorkStatus.WAITING_APPROVAL},
    WorkStatus.ACCEPTED: {WorkStatus.COMMITTED, WorkStatus.REWORK_REQUIRED},
    WorkStatus.COMMITTED: {WorkStatus.STALE},
    WorkStatus.STALE: {WorkStatus.READY, WorkStatus.WAITING_APPROVAL, WorkStatus.CANCELLED},
    WorkStatus.REWORK_REQUIRED: {WorkStatus.READY, WorkStatus.CANCELLED},
    WorkStatus.BLOCKED: {WorkStatus.READY, WorkStatus.CANCELLED},
    WorkStatus.WAITING_APPROVAL: {WorkStatus.READY, WorkStatus.CANCELLED, WorkStatus.ACCEPTED, WorkStatus.REWORK_REQUIRED, WorkStatus.STALE},
}


class TriggerRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)


class DependencyRequest(BaseModel):
    predecessor_id: str
    successor_id: str
    dependency_kind: str = Field(default='artifact', min_length=1)


class RetryRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


class WorkReviewRequest(BaseModel):
    artifact_id: str
    decision: Literal['ACCEPT','REJECT']
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class SubmitRequest(BaseModel):
    content: dict[str, Any]


class TaskDefinition(BaseModel):
    task_id: str
    version: int = Field(gt=0)
    name: str
    objective: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    acceptance_policy: dict[str, Any]
    active: bool = False


class JobDefinition(BaseModel):
    job_id: str
    task_id: str
    task_version: int
    trigger_type: str
    schedule: str | None = None
    event_filter: str | None = None
    timezone: str = 'Asia/Seoul'
    enabled: bool = True


class WorkItem(BaseModel):
    work_id: str
    job_id: str
    task_id: str
    task_version: int
    input_snapshot: dict[str, Any]
    status: WorkStatus
    version: int


class Run(BaseModel):
    run_id: str
    work_id: str
    attempt_no: int
    status: str
    agent_version: str
    model_id: str
    prompt_version: str


class Artifact(BaseModel):
    artifact_id: str
    run_id: str
    kind: str
    uri: str
    sha256: str
    schema_version: str


class Evidence(BaseModel):
    evidence_id: str
    artifact_id: str
    verification_type: str
    input_refs: dict[str, Any]
    result: dict[str, Any]
    verifier_version: str


class MarketIndicator(BaseModel):
    symbol: str
    unit: str
    previous: str
    current: str
    change: str
    change_percent: str


class MarketBriefing(BaseModel):
    task_id: str
    data_mode: str
    model_mode: str
    dataset_version: str
    indicators: list[MarketIndicator]
    citations: list[str]
    interpretation: str
    interpretation_verified: bool


class DocumentUpdatedRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=150)
    document_id: str = Field(min_length=1, max_length=100)
    old_version: int = Field(gt=0)
    new_version: int = Field(gt=0)
    old_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    new_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class PolicyReviewNoteRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=150)
    artifact_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=1000)


class PolicyReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    idempotency_key: str = Field(min_length=1, max_length=150)
    context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision: Literal['REVIEWED','CHANGES_REQUESTED']
    note: str = Field(min_length=1, max_length=1000)


class PolicyReviewReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: Literal['TASK-002']
    data_mode: Literal['DEMO DATA']
    step_no: Literal[4]
    context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision: Literal['REVIEWED','CHANGES_REQUESTED']
    note: str = Field(min_length=1, max_length=1000)
    policy_applied: Literal[False]


class PolicyFollowupVerification(BaseModel):
    task_id: Literal['TASK-002']
    data_mode: Literal['DEMO DATA']
    step_no: Literal[1]
    parent_artifact_id: str
    parent_artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    verification: Literal['DETERMINISTIC_RECHECK']
    semantic_review: Literal['NOT_PERFORMED']


class PolicyIndexReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: Literal['TASK-002']
    data_mode: Literal['DEMO DATA']
    step_no: Literal[2]
    predecessor_artifact_id: str
    document_id: str
    document_version: int = Field(gt=0)
    document_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    index: str
    operation: Literal['CREATED','ALREADY_PRESENT']


class PolicyRegressionReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: Literal['TASK-002']
    data_mode: Literal['DEMO DATA']
    step_no: Literal[3]
    scope: Literal['FIXED_VOC_CLASSIFICATION_ONLY_NOT_POLICY_SEMANTICS']
    predecessor_artifact_id: str
    document_id: str
    document_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    baseline_method: str
    active_method: str
    active_method_state_version: int = Field(gt=0)
    suites: list[dict[str, Any]]


class PolicyChange(BaseModel):
    clause_id: str
    before: str | None
    after: str | None
    citations: list[str]


class PolicyImpact(BaseModel):
    task_id: str
    data_mode: str
    model_mode: str
    document_id: str
    old_version: int
    new_version: int
    changes: list[PolicyChange]
    affected_tasks: list[str]
    followups: list[dict[str, str | int | None]]
    review_status: str


class VocRecommendation(BaseModel):
    recommendation_id: str
    category: str
    count: int
    record_ids: list[str]
    action: str


class VocReport(BaseModel):
    task_id: str
    data_mode: str
    model_mode: str
    source_version: str
    method_version: str
    recommendations: list[VocRecommendation]
    unclassified_ids: list[str]
    source_review_status: str


class VocRecommendationWithHistory(VocRecommendation):
    model_config = ConfigDict(extra='forbid')
    comparison: Literal['SAME_CATEGORY','NEW_CATEGORY','NO_PREVIOUS_REPORT']
    previous_recommendation_id: str | None


class VocReportWithHistory(VocReport):
    model_config = ConfigDict(extra='forbid')
    recommendations: list[VocRecommendationWithHistory]
    previous_artifact_id: str | None
    history_as_of: str
    comparison_basis: Literal['CATEGORY_ONLY_NOT_SEMANTIC']


class VocRecommendationWithSources(VocRecommendationWithHistory):
    document_citations: list[str]


class VocReportWithSources(VocReportWithHistory):
    recommendations: list[VocRecommendationWithSources]
    retrieval_mode: Literal['FIXTURE_DB','OPENSEARCH']


class FeedbackRequest(BaseModel):
    idempotency_key: str = Field(min_length=1,max_length=150)
    artifact_id: str
    recommendation_id: str
    reason: Literal['누락','중복/근거 부족']
    challenger_version: Literal['challenger-v2','challenger-regressed','challenger-overfit'] = 'challenger-v2'


class ActionRequest(BaseModel):
    idempotency_key: str = Field(min_length=1,max_length=150)
