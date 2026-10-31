# Work OS v0.1 작업 결과 보고서

> 작성 기준: 2026-11-01 · 대상 코드: `2110c93`까지의 구현 · 자료: [초기 명세](../init.md), [작업 이력](WORK_STATE.md), 소스와 테스트

## 1. 요약

Work OS는 일회성 모델 실행과 별도로 업무 계약과 작업 상태를 보관하고, 검증된 결과·실패 이력·변경에 따른 후속 작업을 추적하는 로컬 업무 실행 환경이다. 현재 **P0 커널과 P1의 세 가지 가상 시연 경로**가 구현되어 있다. 사용자는 대시보드에서 보고서 생성과 비교, 문서 변경 영향 전파, 개선안 평가와 직접 적용을 확인할 수 있다.

| 영역 | 현재 상태 | 확인 가능한 결과 |
|---|---|---|
| P0 커널 | 구현 및 테스트 | 업무 상태·시도·이벤트 영속화, 중복 방지, 의존성 DAG, 예약·큐·검증 게이트 |
| P1 데모 | 세 경로 구현 및 별도 환경 E2E 확인 | 금융 브리핑, 규정 변경 영향, VOC 개선안 평가·사용자 적용 |
| P2 실제 연계·운영 | 미완료 | 실제 사내 데이터, 운영 보안·관측성, 장애 후 같은 Run 자동 재개는 후속 과제 |

**판단 범위:** 금융 지표, 정책, VOC 및 평가셋은 가상이다. TASK-001에서 실제 LLM 호출을 선택할 수 있지만 원천은 여전히 `DEMO DATA`이며 해석의 의미적 정확성은 검증하지 않는다. 현재 결과는 제품 가능성을 보여주는 로컬 데모이며 운영 적합성이나 실제 업무 품질을 입증하지 않는다.

## 2. 핵심 구조와 구현 내용

### 2.1 업무가 실행보다 오래 남는 구조

| 개념 | 역할 | 현재 구현 |
|---|---|---|
| `TaskDefinition` | 목표·입출력·수락 조건을 가진 업무 계약 | 버전별 정의와 활성 버전 저장 |
| `JobDefinition` | 업무의 실행 계기 | Cron, 이벤트 및 수동 실행 |
| `WorkItem` | 특정 입력에 대해 달성할 지속 작업 | 상태·부모 작업·의존성 및 채택 결과 보관 |
| `Run` | Work를 수행하는 개별 시도 | 성공·실패·재시도를 분리하여 이력 보존 |
| `Artifact` / `Evidence` / `Evaluation` | 결과물과 검증 근거 | 내용·해시·출처·검증기·판정 기록 |
| `Work Event` | 상태 변경 이력 | PostgreSQL의 수정·삭제 불가 이벤트 기록 |

공식 상태와 큐는 PostgreSQL에 저장하고 Redis Streams는 작업 알림에 사용한다. 상태 전이와 이벤트를 함께 기록하며, 중복 요청·동시 요청을 통제한다. DAG 순환을 막고 선행 결과가 바뀌면 관련 후속 작업을 오래된 상태로 전파한다. 실패한 검증 결과는 새 공식 산출물로 채택하지 않으며, 이전에 채택한 유효 결과와 실패 시도는 보존한다.

LangGraph는 Run마다 `inspect_context → draft → finalize` 단계를 수행하고 PostgreSQL에 체크포인트와 단계 로그를 남긴다. 공식 Work 상태와 산출물 채택은 커널 및 별도 검증기가 처리한다.

### 2.2 데이터·출처와 화면

- **PostgreSQL:** 업무·실행·이벤트·검증 근거·산출물 본문과 가상 원천 자료의 저장소. 시장 데이터와 VOC 조회에는 로컬 읽기 전용 역할을 사용한다.
- **OpenSearch:** 시드한 가상 문서를 ID·버전으로 검색하고, 검증 시 PostgreSQL 원본과 내용·해시를 대조한다. 정책 v2 인덱싱은 로컬 데모 인덱스로 한정한다.
- **대시보드:** 세 Task 카드, 실행 상태, 최근 최대 5개 Run의 검증 판정·검사 버전·통과 수 및 DB/Search 상태를 표시한다. 서로 다른 검사 버전의 통과 수는 직접 비교 가능한 품질 점수가 아니다.
- **모델 모드:** 기본은 MOCK. TASK-001은 별도 설정 시 LIVE LLM을 선택할 수 있으며 호출 실패 시 MOCK으로 자동 대체하지 않는다. 키 파일 `.env.apikey`는 Git에서 제외한다.

## 3. 세 가지 시연 흐름

### TASK-001 · 정기 금융 브리핑

**목적:** 동일한 업무를 반복 실행하면서 결과 차이와 검증·실패 이력을 관리한다.

1. 가상 시장 데이터 v1으로 브리핑을 생성하고 지표·출처·Evidence를 확인한다.
2. 데이터 v2를 실행해 두 결과를 비교한다. 예시 지표 USD/KRW는 **1,350 → 1,380, 차이 30**이다.
3. 출처 누락 옵션으로 검증 실패를 만들고, 같은 Work를 재시도해 새 Run에서 통과한다. 실패한 시도는 남는다.
4. 선택적으로 실제 LLM이 해석 문장을 생성한다. 숫자 계산과 출처 검사는 별도로 수행하며 해석의 의미적 타당성은 검증하지 않는다.

구현: [`market.py`](../backend/app/work/market.py), [`service.py`](../backend/app/work/service.py), [`graph.py`](../backend/app/runtime/graph.py) · 테스트: [`test_market.py`](../tests/test_market.py), [`test_search.py`](../tests/test_search.py).

### TASK-002 · 문서 변경과 영향 DAG

**목적:** 문서 변경 이벤트를 단순 검색 답변으로 끝내지 않고 영향받는 업무와 후속 작업으로 연결한다.

1. 가상 `POLICY-VOC` v1에서 v2로의 변경 이벤트를 등록하고 조항·출처·영향 Task를 확인한다. 동일 이벤트는 중복 작업을 만들지 않는다.
2. 변경 해석 재검사 → 로컬 데모 검색 인덱스 갱신 → 고정 VOC 사례 회귀 검사 → 담당자 검토의 네 후속 Work를 생성한다.
3. 선행 검사가 완료되면 사용자가 고정된 문서·산출물·Evidence를 확인하고 검토 완료 또는 보완 요청과 근거를 기록할 수 있다.
4. 이 기록은 사람의 판단 이력이다. **실제 정책을 적용하거나 영향 Task의 `REVIEW_REQUIRED`를 자동 해제하지 않는다.** 근거가 변경되면 이전 판단을 보존하고 재검토한다.

구현: [`policy.py`](../backend/app/work/policy.py), [`service.py`](../backend/app/work/service.py), [`policy_regression.py`](../backend/app/evaluation/policy_regression.py) · 테스트: [`test_policy.py`](../tests/test_policy.py), [`test_policy_index.py`](../tests/test_policy_index.py), [`test_policy_regression.py`](../tests/test_policy_regression.py).

### TASK-003 · VOC 피드백과 개선

**목적:** 새 보고서를 만드는 것과 업무 수행 방법을 평가·개선하는 것을 구분한다.

1. 가상 VOC를 분류해 개선 추천과 VOC·서비스 문서 출처를 기록한다. 문서 ID·버전·해시, SQL 입력과 검색 결과를 검증한다.
2. 추천에 대한 피드백으로 개선안을 만들고 Champion과 Challenger를 **동일한 고정 24건과 별도 회귀 사례 6건**에서 비교한다.
3. 데모의 한 후보는 기본 사례에서 Champion **18/24**, Challenger **24/24**로 계산된다. 별도 회귀셋도 통과해야 하며, 다른 후보는 퇴행으로 차단된다.
4. 평가 통과만으로 활성 방법이 바뀌지 않는다. 사용자가 직접 적용하거나 기각하고, 적용 후 롤백할 수 있다.
5. 이전 검증 보고서와 같은 범주인지 비교하지만, 이는 문장 의미상 중복이나 실제 고객 유용성의 판정이 아니다.

구현: [`voc.py`](../backend/app/work/voc.py), [`improvements.py`](../backend/app/evaluation/improvements.py) · 테스트: [`test_voc.py`](../tests/test_voc.py), [`test_voc_history.py`](../tests/test_voc_history.py), [`test_voc_sources.py`](../tests/test_voc_sources.py).

## 4. 검증 결과와 근거

다음은 기존 구현 작업 중 **실제로 실행하고 기록한 결과**다. 이 보고서 작성 시 테스트를 다시 실행한 것은 아니다.

| 검증 | 결과 | 확인 범위·근거 |
|---|---|---|
| `python -m pytest -q tests` | **45 passed, warning 1건**, 115.08초 | 전용 PostgreSQL·Redis와 로컬 OpenSearch 사용. 상태 전이, 멱등성, 동시 claim, DAG 순환·stale, 발행 게이트 및 각 데모 검사. 경고는 Starlette/anyio deprecation. [기록](WORK_STATE.md) |
| `python -u tests/e2e_stack.py` | 별도 빈 Compose 환경에서 세 시나리오 통과 | 실제 HTTP API·워커로 비교, 검증 실패·재시도, 정책 DAG·검토, VOC 평가·적용·롤백 등을 확인. 최신 대시보드 이력 추가 전 실행된 E2E이며 이후 전체 테스트와 화면을 별도 확인. [시나리오](../tests/e2e_stack.py) |
| `npm run build` | 성공 | Vue/Vite 프런트엔드 빌드. [기록](WORK_STATE.md) |
| `docker compose config --quiet` | 성공 | Compose 설정 확인. [기록](WORK_STATE.md) |
| 브라우저·API 확인 | 성공 | 세 Task 카드의 최근 검증 판정·검사 버전·연결 상태를 표시. [기록](WORK_STATE.md) |
| `python -u tests/live_llm_smoke.py` | 과거 별도 환경에서 실제 호출 성공 | `gpt-6-luna`, `LIVE_LLM`, 가상 원천, `market-validator-v3 PASS` 확인. 해석의 의미 정확성 평가는 아님. [스크립트](../tests/live_llm_smoke.py), [기록](WORK_STATE.md) |
| 체크포인트 재개 테스트 | 유효한 동일 Run을 별도 프로세스에서 재개하는 경로 통과 | 앞서 완료한 단계를 반복하지 않는 내부 경로를 검증. 워커 장애 후 자동 복구 검증은 아님. [`test_graph_runtime.py`](../tests/test_graph_runtime.py) |

테스트 결과는 해당 실행 시점의 환경과 코드에 대한 것이다. 최초 `localhost` 기반 전체 테스트는 IPv6 연결 지연으로 중단했고 통과 건수에 포함하지 않았다. 최종 전체 테스트는 테스트 주소에 `127.0.0.1`을 사용했다.

## 5. 실행과 시연

Docker Desktop 실행 후 저장소 루트에서:

```powershell
docker compose -p workos-p0 up --build -d
```

- 대시보드: <http://localhost:5173>
- API 문서: <http://localhost:8000/docs>
- 세 흐름의 조작 순서와 독립 시연 환경: [3분 데모 가이드](DEMO.md)
- 전체 설치·테스트 명령: [README](../README.md)

별도 빈 환경으로 재현해야 기존 작업 이력과 정책 검토 상태를 건드리지 않는다. 테스트는 지정된 테스트 DB의 테이블을 비우므로 보존할 DB를 대상으로 지정하지 않는다. 서비스 중지 명령은 볼륨을 유지하는 `docker compose -p workos-p0 down`이다.

## 6. 주요 코드와 산출물

| 경로 | 내용 |
|---|---|
| [`backend/migrations/`](../backend/migrations/) | PostgreSQL 스키마·마이그레이션 |
| [`backend/app/domain/contracts.py`](../backend/app/domain/contracts.py) | 상태·요청·산출물 계약 |
| [`backend/app/work/`](../backend/app/work/) | 커널, 상태·DAG·큐, 세 업무, 시드 |
| [`backend/app/api/main.py`](../backend/app/api/main.py) | FastAPI 업무·실행·평가 인터페이스 |
| [`backend/app/runtime/graph.py`](../backend/app/runtime/graph.py) | Run 단위 LangGraph와 단계 기록 |
| [`backend/app/integrations/`](../backend/app/integrations/) | 읽기 전용 SQL, OpenSearch, 산출물 저장소 어댑터 |
| [`backend/app/evaluation/`](../backend/app/evaluation/) | 개선안 평가와 정책 후속 회귀 검사 |
| [`frontend/src/App.vue`](../frontend/src/App.vue) | 업무 대시보드와 세 데모 화면 |
| [`tests/`](../tests/) | 커널·연계·세 흐름 테스트와 E2E 스크립트 |
| [`docs/WORK_STATE.md`](WORK_STATE.md) | 마일스톤별 실제 실행 내역, 한계 및 다음 작업 |

## 7. 남은 과제와 우선순위

1. **장애 복구:** 워커 소유권과 lease fencing을 보강하고 중단된 동일 Run의 안전한 자동 재개를 구현·검증한다. 현재 lease 만료 시 기존 Run을 실패로 남기고 새 Run을 만든다. 외부 모델 호출의 정확히 한 번 실행도 보장하지 않는다.
2. **실제 읽기 전용 데이터 연계:** 실제 사내 PostgreSQL·문서 검색을 명시적으로 설정할 수 있는 어댑터와 원천 변경 감지·복구를 검증한다. 현재 SQL과 검색 자료는 가상 데모다.
3. **운영 준비:** 인증·권한, 운영 관측성, 장시간 실행과 동시성 부하, 배포 보안 등을 별도로 검증한다. 현재는 단일 사용자 로컬 구성이다. 마이그레이션의 down SQL도 실행 검증되지 않았다.

검증기의 PASS는 해당 시점의 형식·수치·출처·무결성 검사 결과다. 모델의 해석이나 추천이 의미적으로 옳은지, 정책이 법규에 부합하는지, 합성 평가의 점수가 실제 고객에게 유용한지는 별도 검증이 필요하다.
