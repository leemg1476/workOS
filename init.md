# Work OS — 프로젝트 초기화 명세 (init.md)

> 버전: 0.1 (MVP/시연용) · 작성일: 2026-10-31 · 상태: 구현 전 설계안
>
> **한 문장 정의:** Work OS는 인간과 AI가 수행하는 업무를 영속적인 작업 계약으로 등록하고, 일정·데이터 변경·평가 결과에 따라 실행하며, 검증된 산출물과 실패 이력을 축적해 업무 수행 방법을 지속적으로 개선하는 운영체제다.

## 0. 이 문서를 읽는 개발 에이전트에게

이 문서는 신규 Work OS 프로젝트의 초기 구현 계약이다. 별도 지시가 없다면 **아래의 3개 시나리오가 로컬에서 처음부터 끝까지 시연되는 MVP**를 목표로 한다.

1. 우선 저장소 현황을 확인하고, 기존 코드와 변경사항을 보존한다.
2. `TaskDefinition → JobDefinition → WorkItem → Run → Artifact/Evidence → Evaluation`의 영속성 및 관계를 먼저 구현한다.
3. 복잡한 범용 에이전트 플랫폼보다 3개 시나리오의 **작업 이력·검증·이전 결과 대비 변화·재실행**을 먼저 구현한다.
4. 실서비스 인증정보나 사내 DB가 없어도 실행할 수 있도록, `MOCK` 모드의 시드 데이터와 검증기를 기본 제공한다. `LIVE` 모드에만 LLM/API 키와 실제 연계를 사용한다.
5. 샘플 시연과 실제 AI 추론 결과를 UI 및 로그에서 명확하게 구분한다. 시뮬레이션 결과를 실제 모델 성능이라고 주장하지 않는다.
6. 동작하는 최소 수직 기능이 완성될 때마다 테스트를 수행하고, 확인 가능한 결과·남은 위험·향후 작업을 기록한다.

### 이번 버전의 성공 정의

사용자가 UI에서 **① 정기 보고서 생성 → ② 외부 변경 이벤트에 따른 영향 전파 → ③ 평가·비교 후 개선 버전 적용**이라는 세 흐름을 직접 확인하고, 일반적인 단발성 에이전트와의 차이를 이해할 수 있어야 한다.

---

## 1. 기존 AX 에이전트 개발과 반드시 달라야 하는 점

| 비교 항목 | 기존 에이전트 중심 AX | Work OS MVP |
|---|---|---|
| 기본 단위 | 특정 Agent/Graph와 API | 검증 계약을 가진 `TaskDefinition` |
| 실행 원인 | 사용자 채팅·API 호출 | Cron, 이벤트, 사용자 요청, 평가 결과 |
| 실행 식별 | Session 중심 | Task/Job/WorkItem/Run을 연결 |
| 결과물 | 현재 요청의 텍스트 또는 JSON | 버전·출처·검증 상태가 있는 Artifact |
| 지식 보존 | 대화 메모리·Vector DB | 검증 결과·실패·작업 의존성·산출물 |
| 품질 관리 | 개발 단계 E2E 테스트 | 매 Run 평가 + 버전 간 회귀 테스트 |
| 변경 반영 | 개발자가 수동 재개발 | 영향 Work 생성, 개선안 제안, 사용자 적용 후 승격 |
| 화면의 중심 | Agent 목록·채팅창 | 업무 목록·실행 이력·품질 변화·작업 상태 |

**차별화 규칙:** 단순히 LangGraph로 보고서를 생성하거나 Cron에 연결한 것만으로는 Work OS 구현으로 인정하지 않는다. **작업 계약, 재현 가능한 실행 기록, 검증 결과, 후속 작업 및 개선 루프**가 화면과 API에서 확인되어야 한다.

### 개념 경계

- **TaskDefinition**: 재사용 가능한 업무 명세(목표, 입력, 산출물, 수락 조건, 버전).
- **JobDefinition**: Task를 언제/왜 실행할지 정의한 Cron·이벤트·수동 트리거.
- **WorkItem**: 특정 입력·데이터 버전에 대해 달성해야 하는 영속적 작업 인스턴스. 필요하면 하위 작업 DAG를 가진다.
- **Run**: WorkItem을 해결하기 위한 개별 실행 시도. 재시도·모델 교체로 여러 Run이 생길 수 있다.
- **Artifact**: 보고서, JSON, SQL, 코드, 차이 리포트 등 결과물.
- **Evidence/Evaluation**: 결과가 요구사항을 충족한다는 검증 근거와 평가 결과.
- **ImprovementProposal**: 프롬프트·검색·작업 분해·도구·검증 방식 변경 후보. 검증 없이 운영 버전을 교체하지 않는다.

## 2. 제품 시연: 처음 화면에 반드시 보일 3개 Task

데모 홈에는 다음 카드 3개를 고정 제공한다. 각 카드에는 **실행 버튼, 최근 Run, 지난 결과와의 차이, 검증 점수, 연결된 시스템**을 표시한다.

### TASK-001: 일일 금융시장 리스크 브리핑 — 정기 Job

**보여주려는 차이:** 일반 에이전트는 호출할 때 보고서를 한 개 생성한다. Work OS는 매 영업일 같은 업무 계약을 실행하고, 데이터 변경·이전 결과·검증 실패를 관리하며, 보고서 품질의 추세를 보여준다.

| 항목 | 정의 |
|---|---|
| 업무 목표 | 주요 금융시장 지표 변동을 분석하고 근거가 연결된 브리핑을 발행 |
| 트리거 | `Asia/Seoul` 기준 영업일 08:00 Cron + '지금 실행' 버튼 |
| 입력 | PostgreSQL 시장 지표/환율 시계열, OpenSearch 뉴스·리서치 문서 |
| 에이전트 | Data Collector → Calculator → Retrieval/Analyst → Writer → Reviewer |
| 산출물 | 요약, 핵심 지표 테이블, 원인 가설, 근거 문서 ID, 전회 대비 변경점 |
| 검증 | SQL 계산 결과 일치, 수치 재계산, 출처 존재, JSON Schema, 누락 지표 검출 |
| 지속 자산 | Run별 데이터 기준 시각, 조회 SQL/결과 해시, 인용 ID, 평가 결과, 오류 분류 |
| 연계 | Source PostgreSQL, OpenSearch, LLM, Object Store, Scheduler |

**필수 시연 단계**

1. 시드된 시장 데이터로 첫 Run을 실행하고 브리핑 v1과 Evidence를 표시한다.
2. 데모 데이터의 특정 환율 지표를 변화시키고 두 번째 Run을 실행한다.
3. '전회 대비 변화'에서 숫자와 분석의 차이, 변경 근거를 보여준다.
4. 한 개의 인용 문서를 의도적으로 제거한 테스트를 실행해 `VERIFICATION_FAILED`가 되는 모습을 보여준다.
5. 수정·재실행 후 통과한 Artifact만 발행한다. 이전 실패 Run도 보존한다.

**완료 기준:** 같은 Task에서 Run이 2개 이상 기록되고, 이전 결과 차이·검증 실패·재검증 성공·출처 링크를 UI에서 확인할 수 있다.

### TASK-002: 내부 규정 변경 감지와 영향 분석 — 이벤트 기반 Work DAG

**보여주려는 차이:** 일반 RAG 에이전트는 변경된 규정을 묻는 질문에 답한다. Work OS는 문서 변경 자체를 이벤트로 받아 관련 업무·에이전트·테스트를 추적하고 **후속 작업을 자동 생성**한다.

| 항목 | 정의 |
|---|---|
| 업무 목표 | 내부 정책/업무 매뉴얼 변경을 탐지하고 영향받는 AX 업무의 조치 목록 작성 |
| 트리거 | `document.updated` Webhook 또는 수동 '변경 이벤트 발생' |
| 입력 | 변경 전후 규정 문서, OpenSearch 문서 인덱스, Work 의존성 레지스트리 |
| 에이전트 | Diff Extractor → Impact Analyst → Work Planner → Validator |
| 산출물 | 조항별 변경 요약, 영향 대상 Task, 근거, 후속 WorkItem DAG |
| 검증 | 문서 버전 일치, 인용 위치 확인, 의존성 그래프의 유효성, 중복 Work 방지 |
| 지속 자산 | 변경 이벤트 ID, 이전/신규 문서 해시, 영향 분석, 생성된 조치와 검토 기록 |
| 연계 | Document Repository, Webhook/Event Bus, OpenSearch, Work Registry |

**필수 시연 단계**

1. 규정 문서 v1을 조회하고 기존 Task가 해당 문서에 의존함을 확인한다.
2. v2 변경 이벤트를 주입한다.
3. 시스템이 변경 조항을 비교하고 영향받는 Task를 `STALE` 또는 `REVIEW_REQUIRED`로 표시한다.
4. `변경 해석 검증 → 검색 인덱스 갱신 → 회귀 테스트 → 담당자 검토` 등 하위 WorkItem을 생성한다.
5. 검토가 필요한 변경은 `REVIEW_REQUIRED`로 표시하고, 운영 설정을 자동 변경하지 않는다. 별도 승인 서비스나 결재 시스템은 구현하지 않는다.

**완료 기준:** 이벤트 한 번으로 2개 이상의 연결 WorkItem과 의존성 DAG가 생성되고, 기존 Task의 재검증 필요 상태 및 검토 대상이 보인다.

> 데모 정책 문서는 가상의 내부 규정으로 한정한다. 실제 법규 해석·금융 의사결정 자동화의 정당성을 주장하지 않는다.

### TASK-003: 고객 VOC 분석·개선과제 추천 — 평가 기반 개선 Job

**보여주려는 차이:** 일반 분석 에이전트는 VOC를 요약하고 개선안을 추천한다. Work OS는 **추천이 실제로 유용했는지 피드백을 수집하고, 다음 버전의 분석 방법을 평가한 뒤 승격**한다.

| 항목 | 정의 |
|---|---|
| 업무 목표 | 고객 VOC를 분류하고 우선순위가 있는 개선과제 3개를 근거와 함께 제안 |
| 트리거 | 주간 Cron + `feedback.received` 이벤트 + 수동 실행 |
| 입력 | PostgreSQL의 익명 VOC 테이블, OpenSearch 상품/서비스 문서, 이전 추천과 리뷰 |
| 에이전트 | VOC Grouper → Evidence Retriever → Recommendation Analyst → Evaluator |
| 산출물 | 주요 이슈, 개선과제 TOP 3, 근거 VOC ID, 기존 추천과 중복 여부 |
| 검증 | VOC 수치 일치, 근거 포함, 중복 감지, 개인정보 패턴 검사, 평가셋 점수 |
| 지속 자산 | 추천-근거-사용자 리뷰 연결, 실패 사례, 실험 버전, 회귀 평가 |
| 연계 | VOC DB, OpenSearch, LLM, Feedback API, Evaluation Dataset |

**필수 시연 단계**

1. 기본 분석 파이프라인(Champion)으로 주간 VOC 보고서와 개선 추천을 생성한다.
2. 담당자가 한 추천에 `중복/근거 부족` 피드백을 등록한다.
3. ImprovementProposal이 생성되고 Challenger가 프롬프트·검색 전략 중 **하나만** 수정한다.
4. 동일한 고정 평가셋(샘플 20건 이상)을 Champion/Challenger에 재실행한다.
5. 기준을 통과한 개선안에만 Work OS 화면의 `적용` 버튼을 활성화한다.
6. 사용자가 `적용`을 누르면 다음 Run에 새 버전을 사용하고, 이전 버전 및 롤백 경로를 보존한다. 별도 승인·결재 시스템은 없다.

**완료 기준:** 품질 문제 → 개선 제안 → 동일 조건 비교 평가 → 사용자 적용/기각 → 새 버전 Run의 전체 연결 이력이 보인다. 자동 평가 점수만으로 무조건 승격하지 않는다.

---

## 3. 전체 서비스 구성과 연계 시스템

### 3.1 논리 구조

```text
User / Demo UI / External Event
           │
      Work OS API
           │
   ┌───────┴───────────────────────────────────┐
   │ Work Kernel                               │
   │ Registry / Scheduler / Dependency DAG     │
   │ Event Ledger / Context Builder / Policies │
   └───────┬───────────────────────────────────┘
           │
   Agent Runtime (LangGraph) ─── LLM Provider
           │
   Connectors (SQL / Search / Document)
           │
   Artifacts + Evidence → Evaluator → Apply/Reject
           │
   Run History / Execution Logs / Improvement Proposals
```

### 3.2 연계 시스템 매트릭스

| 연계 시스템 | 권장 기술/연계 규약 | 이번 MVP | 역할 및 인터페이스 |
|---|---|---|---|
| Frontend | Vue 3 + TypeScript | **필수** | 3 Task 카드, Run 비교, Artifact, DAG, 적용/기각 화면 |
| Work OS API | Python FastAPI + Pydantic | **필수** | Task/Job/Run/Artifact/Evaluation/Improvement REST API |
| Work/Metadata DB | PostgreSQL | **필수** | 원본 작업 상태, 버전, 의존 관계, 이벤트, 평가 |
| Agent Runtime | LangGraph Python | **필수** | 실행 단위별 노드/도구 호출, 체크포인트, 재개 |
| Trigger/Scheduler | APScheduler 또는 DB-backed scheduler | **필수** | Cron, 수동 실행, 이벤트 트리거; 중복 실행 방지 |
| Work Queue | Redis Streams/RQ/Celery 중 1개 | **필수** | 비동기 실행과 워커 분리; 재시도/동시성 한도 |
| Domain SQL | 샘플 PostgreSQL 별도 schema | **필수(시드)** | 금융지표·VOC 데이터, 읽기 전용 Connector |
| Vector / Hybrid Search | OpenSearch | **필수(시드)** | 규정·뉴스·상품문서 검색, 문서 ID/버전 반환 |
| Document Repository | 로컬 파일/MinIO, 추후 사내 DMS | **필수(시드)** | 규정 및 보고서 파일의 버전/해시 |
| Artifact Storage | MinIO(S3 호환) 또는 로컬 개발 어댑터 | **필수** | 실행 결과물·평가셋·원본 스냅샷 보존 |
| LLM Gateway | OpenAI/Anthropic 또는 사내 vLLM 어댑터 | **LIVE 모드** | 모델 라우팅, 비용/토큰, 요청 추적 |
| 실행 로그 | Work OS DB + Python structured logging | **필수** | run_id, 단계, 모델/도구 호출, 지연·오류·토큰 사용량 |
| 버전 관리 | Work OS DB 내 TaskDefinition/Prompt/Graph 버전 | **필수** | 개선 전후 버전 비교, 승격, 롤백 |
| Event Integration | Webhook, 추후 Kafka/사내 ESB | **Webhook 필수** | document.updated, feedback.received, data.updated |
| Monitoring | Prometheus/Grafana 또는 기존 관제 | **운영 권장** | 지연, 실패, 비용, Job 성공률, 품질 변화 |
| Reverse Proxy | Nginx | **배포 단계** | API 라우팅, TLS, 사내망/외부 연계 경계 |

**연계 범위의 원칙**

- 샘플 DB와 가상 Webhook으로 3개 데모는 모두 실행 가능해야 한다.
- 사내 DB/OpenSearch의 실제 접속 정보는 코드에 넣지 않고 환경변수 또는 Secret Manager를 사용한다.
- Domain DB는 기본 **읽기 전용**이다. 이번 MVP에는 실제 업무 데이터에 대한 변경 기능을 구현하지 않는다.
- 실행 추적의 기본 식별자는 Work OS 자체의 `run_id`다. 별도 추적·관제 서비스에 의존하지 않는다.
- 임베딩과 검색 인덱스는 재구축 가능한 검색 계층이며, 검증된 Work의 원본 상태는 PostgreSQL/Artifact 저장소가 소유한다.
- 외부 시스템 호출은 timeout, idempotency key, retry 정책을 기록한다. 환불·배포·결재 등 실세계 부작용이 있는 동작은 MVP 범위에서 제외한다.

## 4. 데이터 모델과 핵심 불변식

### 4.1 최소 테이블/관계

| 테이블 | 핵심 필드 |
|---|---|
| `task_definitions` | task_id, version, objective, input_schema, output_schema, acceptance_policy, active |
| `job_definitions` | job_id, task_id/version, trigger_type, schedule/event_filter, timezone, enabled |
| `work_items` | work_id, job_id, task_version, input_snapshot_id, status, parent_work_id, priority |
| `work_dependencies` | predecessor_id, successor_id, dependency_kind, pinned_artifact_version |
| `runs` | run_id, work_id, attempt_no, agent_version, model_id, prompt_version, status, timestamps |
| `artifacts` | artifact_id, run_id, kind, uri, sha256, schema_version, created_at |
| `evidence` | evidence_id, artifact_id, verification_type, input_refs, result, verifier_version |
| `evaluations` | evaluation_id, run_id, eval_suite_version, metric_results, verdict |
| `improvement_proposals` | proposal_id, task_id, changed_component, champion_version, challenger_version, evaluation_id, status |
| `work_events` | event_id, event_type, subject_id, causation_id, actor, payload, timestamp |

`TaskDefinition` 버전과 `JobDefinition`의 실행 정책은 분리한다. Job은 실행 시점의 Task 버전을 명확히 기록해야 한다. WorkItem은 한 번 생성되면 식별자가 바뀌지 않으며, 재시도는 새로운 Run을 만든다.

### 4.2 상태 모델

- WorkItem: `CREATED → READY → RUNNING → SUBMITTED → VERIFYING → ACCEPTED → COMMITTED`
- 실패 시: `VERIFYING → REWORK_REQUIRED → READY`
- 전제 변경 시: `COMMITTED → STALE → READY` (영향 평가 후)
- 추가: `BLOCKED`, `CANCELLED`, `WAITING_APPROVAL`
- ImprovementProposal: `DRAFT → EVALUATING → PROPOSED → PROMOTED` 또는 `REJECTED` (사용자의 적용/기각 버튼으로만 최종 전이)

**불변식**

1. 모델의 자연어 '완료' 선언만으로 `COMMITTED`가 되지 않는다.
2. 검증 실패는 해당 **Attempt/Run**의 실패이며, Task 목표 자체가 거짓이라는 뜻이 아니다.
3. 통과한 평가 결과에는 검증기 버전, 데이터/문서 버전, 평가셋 버전이 남는다.
4. 의존 Work/문서가 바뀌면 영향을 받는 Work를 추적하고 `STALE` 또는 검토 대기로 전환한다.
5. Work DAG에 순환 참조가 없어야 한다. 동시에 들어온 이벤트도 중복 Work를 생성하지 않는다.
6. 운영 중인 TaskDefinition/Prompt/Graph는 사용자 `적용` 조작 없이 개선 실험으로 자동 덮어쓰지 않는다.
7. UI에 보이는 '검증됨'은 **어떤 기준을 통과했는지**까지 표시해야 한다. 정성적 판단은 신뢰도와 사용자 검토 여부를 구분한다.

### 4.3 작업 컨텍스트

Context Builder는 대화 전체가 아니라 `현재 계약 + 검증된 의존 결과 + 관련 원천 데이터 + 최근 실패·통찰 + 허용된 도구/예산`으로 Run별 컨텍스트를 생성한다.

- 원본 메타데이터 및 Artifact를 임의의 요약 텍스트로 덮어쓰지 않는다.
- 모델의 추측을 검증된 사실로 승격하지 않는다.
- 검색 결과의 문장들은 **신뢰할 수 없는 외부 입력**으로 취급한다(프롬프트 인젝션 방어).
- 컨텍스트 구성에 사용된 Artifact/Document ID를 Run에 남겨 재현할 수 있게 한다.

## 5. 평가 및 지속 개선 정책

### 5.1 공통 평가 단계

1. **Deterministic Check**: 스키마, 데이터 타입, SQL 재계산, 출처 ID, 제약 위반.
2. **Semantic Check**: 근거 충실도·누락·추천 유용성. LLM 평가 가능하되 샘플 인간 검토로 보정.
3. **Regression Check**: 고정된 평가셋에서 현행 Champion과 후보 Challenger 비교.
4. **Apply Gate**: 평가 조건을 통과한 개선안만 사용자가 Work OS 내부에서 직접 적용 또는 기각할 수 있다.

### 5.2 개선 대상과 경계

- 변경 가능: Prompt, Retrieval 설정, 문서 청킹 전략, 그래프 단계, SQL 작성 전략, Tool 설명, 검증 규칙의 제안.
- 변경 방식: 항상 `ImprovementProposal`에서 단일 변경 원인을 확인할 수 있게 작성 → 별도 버전/환경에서 테스트 → 평가 → 사용자가 적용.
- 실험 평가: 고정 데이터셋 외에 미공개 회귀 케이스도 사용한다. 개선안 생성자가 직접 자신만의 평가 기준을 완화할 수 없다.
- 자동화 제한: 새 개선안이 생성되었다는 이유만으로 운영 시스템에 즉시 배포하지 않는다.
- 초기 데모 승격 예시: 주요 정확성 지표 비하락, 중대 제약 위반 0건, 목표 지표 개선, 사용자 적용. 임계값은 데모 기준이며 실제 사업별 보정 필요.

**지속 개선의 의미:** 새 데이터로 보고서를 다시 만드는 행위와, 같은 입력에서 더 좋은 방법으로 일을 수행하게 되는 행위를 명확히 구분한다. 후자만 개선 실험으로 기록한다.

## 6. 최소 API 계약

| Method | Endpoint | 의미 |
|---|---|---|
| GET | `/api/tasks` | Task 목록/현행 버전/품질 추세 |
| POST | `/api/tasks/{task_id}/run` | 수동 Job 트리거 및 Work 생성 |
| GET | `/api/jobs` | Cron/이벤트 Job 목록·최근 상태 |
| GET | `/api/work/{work_id}` | 작업 상태 및 DAG |
| GET | `/api/runs/{run_id}` | 실행 이력·단계별 로그·입력 버전·비용 |
| GET | `/api/runs/{run_id}/artifacts` | 결과와 근거 |
| GET | `/api/tasks/{task_id}/compare?left=...&right=...` | 두 Run 산출물/평가 차이 |
| POST | `/api/events/document.updated` | TASK-002 변경 이벤트 |
| POST | `/api/feedback` | TASK-003 피드백 등록 |
| GET | `/api/improvements` | 개선안/Champion-Challenger 비교 |
| POST | `/api/improvements/{id}/apply` | 평가를 통과한 개선안 적용 |
| POST | `/api/improvements/{id}/reject` | 개선안 기각 |
| GET | `/api/health` | 서비스 및 커넥터 상태 |

API 요청·응답은 OpenAPI로 공개하고, 주요 요청에는 `request_id`/`work_id`/`run_id`를 일관되게 포함한다. 쓰기 요청에는 권한·멱등성 검사가 필요하다.

## 7. 사용자 화면: 3분 데모 시나리오

### 화면 1. Work Dashboard

- Task 3개 카드: 이름, 실행 주기, 연결 DB/Search, 최근 Run 상태, 품질 추세.
- Agent 목록이나 채팅창보다 **업무 결과와 검증 상태**가 먼저 보여야 한다.

### 화면 2. TASK-001 — 반복 실행과 차이

- [첫 실행] → Artifact v1 → [샘플 데이터 변경] → [재실행] → 차이 비교.
- 숫자 재계산·출처 검증 결과와 실패한 실행 기록을 표시한다.

### 화면 3. TASK-002 — 변경 영향 전파

- [문서 v2 이벤트 발생] → 변경 전후 조항 비교 → 영향받는 Task/Work DAG → 검토 필요 상태.
- 이 화면이 **단순 RAG 답변과의 차이**를 보여주는 핵심 장면이다.

### 화면 4. TASK-003 — 지속 개선

- [VOC 분석] → [추천 피드백] → 개선안 생성 → Champion/Challenger 점수 비교 → [적용/기각].
- 새로운 버전이 적용된 다음 Run과 기존 버전으로 복귀하는 동작을 보여준다.

UI에서 모든 MOCK 결과는 `DEMO DATA`로 표시한다. 외부 연계가 비활성화되었으면 숨기지 말고 연결 상태를 보여준다.

## 8. 구현 단계와 검증 체크리스트

### P0 — Kernel & 데이터 계약

- [ ] Docker Compose로 API, UI, PostgreSQL, Redis, OpenSearch, 저장소 실행
- [ ] Task/Job/WorkItem/Run/Artifact/Evidence 스키마 및 마이그레이션
- [ ] 상태 전이와 DAG 순환 방지, 이벤트/중복 실행 처리
- [ ] MOCK/LIVE 모드와 샘플 데이터 시딩
- [ ] 최소 Work Dashboard

### P1 — 세 가지 동작하는 데모

- [ ] TASK-001: 2회 실행, 데이터 변화, 비교 리포트, 검증 실패/복구
- [ ] TASK-002: 문서 변경 이벤트, 영향 DAG, 검토 필요 상태
- [ ] TASK-003: 피드백 등록, 개선안, 고정 평가셋, 적용/기각·버전 승격
- [ ] 각 Run에서 결과물·검증 결과·데이터 출처·단계별 실행 로그 확인

### P2 — 실제 시스템 연계

- [ ] 사내 PostgreSQL 읽기 전용 Connector
- [ ] OpenSearch 문서/벡터/하이브리드 검색
- [ ] 자체 Run/Step 로그 검색과 진단 화면
- [ ] DB 기반 Prompt·Graph·검증기 버전 관리와 롤백
- [ ] 오류/성능 지표, 테스트 자동화, TLS/Proxy(운영 배포 시)

### 테스트

**단위 테스트**
- [ ] 허용되지 않은 Work/Improvement 상태 전이 거부
- [ ] 동일 트리거의 중복 Work 생성 방지
- [ ] DAG 순환 참조 거부
- [ ] 외부 데이터/문서 버전 변경 시 영향 계산
- [ ] 검증 실패 Artifact의 자동 발행 거부

**통합 테스트**
- [ ] 샘플 SQL 조회·OpenSearch 검색·Artifact 저장 왕복
- [ ] 실패한 Run 재시도 시 새 Run ID 생성 및 이력 보존
- [ ] 동시 실행이 Work의 공식 상태를 손상시키지 않음
- [ ] 외부 도구 일시 장애 후 재시도/중단 및 오류 원인 기록

**E2E 테스트**
- [ ] 데모 1: 일일 보고서 v1/v2 비교, 오류 재검증
- [ ] 데모 2: 문서 변경 이벤트 → 영향 Work → 검토 필요
- [ ] 데모 3: 피드백 → Challenger → 평가 → 승격/기각

## 9. 개발·운영의 비기능 원칙

- **권한:** 최소 권한, 읽기 전용 데이터 연계, 민감정보 마스킹. 초기 데모는 단일 사용자이며 별도 인증·승인 서비스를 만들지 않는다.
- **관측:** Task/Job/Work/Run ID 상관관계, 단계별 실행 로그, 비용, 실패 원인, 품질 추세 기록.
- **재현:** 데이터/문서 버전, 모델, 프롬프트, 그래프, 검증기, 평가셋 버전 보존.
- **안정성:** 비동기 작업의 재시작·재시도·중복 요청 처리, 작업별 시간·토큰 예산.
- **분리:** 각 Run의 임시 작업공간 격리; Artifact 원본 저장소와 검색 인덱스 분리.
- **인간 통제:** 개선 버전의 적용/기각은 Work OS 화면에서 사용자가 직접 선택한다. 실제 업무 데이터 쓰기·배포·결재는 구현하지 않는다.
- **간결성:** 다중 에이전트가 불필요한 단계에는 단일 모델/결정론적 코드 사용. 범용 기능은 3개 데모에서 필요해질 때 확장한다.

## 10. 이번 버전에서 하지 않을 것

- 범용 자율 AGI 운영체제 구현
- 모든 업무 도메인의 온톨로지 및 범용 지식 그래프 구축
- 사내 실제 데이터를 데모용으로 무단 복제
- 학습용 모델 파인튜닝 또는 가중치 자체의 자율 수정
- 검증 또는 사용자 적용 없는 운영 Prompt/Graph/Verifier 자동 변경
- 실제 금융 투자·규제·고객에게 영향을 주는 결정을 무인으로 실행
- 복잡한 멀티테넌트 과금/Marketplace/대규모 에이전트 스웜

## 11. 최초 실행·인수 시 제출할 산출물

1. `docker-compose.yml` 및 환경변수 예시, 시작/중지 방법.
2. PostgreSQL 마이그레이션과 TASK-001~003 재현용 시드 스크립트.
3. Work OS API OpenAPI 문서와 3개 데모의 실행 명령/API 호출 예시.
4. Vue 대시보드 및 3개 데모 화면.
5. 최소 단위·통합·E2E 테스트 결과와 알려진 제한사항.
6. 작업 실행 이력, 검증·개선 경로를 설명하는 1페이지 운영 가이드.

### 사용자 확인이 필요한 결정

| 선택 | 이번 초기안 | 대안 및 결정 시점 |
|---|---|---|
| 주력 데모 도메인 | 금융시장·내부 규정·VOC | 실제 사내 고객 데이터 도메인은 연계 단계에서 선정 |
| API/UI | FastAPI + Vue 3 | 사내 표준이 Spring Boot면 착수 전 변경 |
| Scheduler | 단순 DB-backed Cron/Queue | 장기 실행·복구 요구가 커지면 Temporal 검토 |
| LLM | 공급자 교체 가능한 Adapter | 사내 vLLM/OpenAI/Anthropic 중 실제 공급자는 환경별 선택 |
| 검증 방식 | 결정론적 검사 + 선택적 LLM 평가 + 사용자 적용/기각 | 업무별 평가셋 구축 후 임계값 확정 |
| 배포 | 로컬 Docker Compose 우선 | Nginx/AP 서버 배포는 별도 단계 |

---

## 최종 인수 질문

첫 시연을 본 사용자가 아래 질문에 모두 **예**라고 답할 수 있어야 한다.

1. *에이전트를 호출한 것*이 아니라 *업무를 등록한 것*처럼 보이는가?
2. 동일 업무가 여러 번 실행된 결과와 품질의 변화를 확인할 수 있는가?
3. 데이터/문서 변경이 후속 작업을 만들어내는 것을 볼 수 있는가?
4. AI의 결과를 어떤 검증 근거로 신뢰할 수 있는지 확인할 수 있는가?
5. 실패·피드백이 다음 실행 방법의 개선안으로 연결되는가?
6. 개선을 적용하지 않거나 이전 버전으로 되돌릴 수 있는가?

**이 여섯 가지가 Work OS v0.1의 제품 정의이자 수락 기준이다.**
