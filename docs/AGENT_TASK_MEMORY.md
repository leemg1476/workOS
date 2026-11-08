# 하나의 실행 경로와 태스크별 기억

## 2026-11-08: 지속 지식 v3

아래의 최근 결과/실패/검토 창과 별개로, 승인한 지식을 영속적으로 저장한다. 태스크 작업실의 **오래 유지할 태스크 지식**에서 후보의 원문·계산 미리보기와 출처 보고서를 확인하고 사유와 함께 Apply/Reject/Retire한다. 모델은 이 결정을 대신하지 않는다. 지식은 태스크 버전과 정확한 입력 해시에 묶이며, 승인된 범위만 이후 Run에 고정된다. 이미 실행한 Run의 기억은 바뀌지 않는다. 활성 지식은 같은 범위20개/본문24,000자로 제한하고 초과 승인을 거부한다. 오래된 지식을 조용히 버리지 않는다.

- `LESSON`: 사람이 승인한 작업 절차. 사실 검증의 결과가 아니다. AI 의견은 자동으로 PROPOSED 후보가 되며 실제 작성 프롬프트에서는 미승인 AI 의견을 제외한다. 기존 의견도 멱등 수입할 수 있다.
- `SOURCE_EXCERPT`: 저장된 원문에 정확한 인용 문자열이 있는지 확인한다. 해석의 진위를 증명하지 않는다. 같은 범위의 새 원문에서 인용이 사라지면 관련 확정 Work를 STALE로 만들고 대기 결과의 승인을 차단한다.
- `CALCULATION`: JSON 입력 경로와 제한된 사칙연산 AST를 Decimal로 계산한다. 모델 대신 백엔드가 계산표를 만든다. 입력 숫자가 현실을 대표하는지, 본문의 모든 숫자가 맞는지까지 검증하지 않는다.
- `CHECKLIST`: 최종 단계에 정해진 ID의 본문을 요구한다. 누락/중복/빈 본문은 실패로 남기며 성공 보고서로 게시하지 않는다. 항목의 의미적 충족 여부는 사람 검토가 필요하다.

검색 응답의 인용 URL 중 최대3개를 허용 도메인에서 생성 후 읽고 URL·본문·해시·읽은 시각을 보존한다. HTML/plain만 지원하며 PDF/실패는 UNAVAILABLE이다. 이는 생성 당시 검색 엔진 원문 스냅샷이나 독립 사실 검증이 아니다. 같은 입력의 최근 원문 발췌와 승인 지식은 작성자에게, 실제 원문·계산·보고서는 평가자에게 전달한다. 같은 모델의 별도 호출이 독립 전문가가 되지는 않는다. `WORKOS_REVIEW_MODEL`을 선택해도 평가는 참고 의견이며 자동 채택을 하지 않는다.

구현: `work/knowledge.py`, `integrations/source_snapshots.py`, `evaluation/report_quality.py`, 마이그레이션024. API: `GET/POST /api/tasks/{id}/knowledge`, `POST /api/tasks/{id}/knowledge/import-feedback`, `POST /api/knowledge/{id}/decision`, `GET /api/runs/{id}/review-context`. 현재 버전은 task-memory-v3, provided-text-step-v3, 일반 구조 검사v5, 연결/결합 검사v3이며 아래 v2 설명은 이전 구현 기록이다.

기능 검증과 LIVE 합성 입력2회 확인의 범위는 [실행 근거](verification/task-knowledge-20261108/README.md)를 참고한다. 기존42회 연구는 중단 상태를 보존하며 다시 실행하지 않았다.

2026-11-07 사용자 목표에 따라 기본 작업실을 태스크 선택 → 기억 확인 → 이어하기 → 결과/근거 조회로 정리했다. 등록·연결 설정은 기본적으로 접힌다. 태스크별 에이전트 프로세스나 별도 채팅 기억 저장소는 만들지 않는다. 기존 공통 큐·Kernel·worker가 등록한 업무 실행기를 호출한다.

## 실행 흐름

1. 등록한 Task/Job이 영속적인 Work를 만든다. 공통 worker가 큐에서 하나의 Run을 claim한다.
2. claim 트랜잭션 안에서 `run_task_memories`에 같은 태스크 버전의 기억을 고정하고 `run.memory_pinned` 이벤트를 기록한다. 기억 저장 실패는 claim 전체를 롤백한다.
3. 텍스트 실행은 고정 기억을 컨텍스트와 각 단계 모델 입력에 사용한다. 기억은 과거 자료이며 현재 원천·계약을 바꾸는 지시가 아니다.
4. 검증 통과 결과를 정해진 정책에 따라 확정하거나 사람 검토에 보낸다. 실패와 사용자 보완 의견은 다음 실행에서 참고한다.
5. 연결·rate limit·일시적 서버 오류는 5초/10초 backoff로 같은 Work에 총 3회까지 새 Run을 만든다. 예산 소진·검증 실패·사용자 보완 요청에는 명시적 이어하기를 제공한다. 워커 lease 만료의 기존 복구도 유지한다.
6. 방법의 개선은 기존 고정 사례 비교와 사용자 적용/기각·롤백 경로를 사용한다. 기억을 참고했다는 사실을 품질 향상으로 표시하지 않는다.

## 기억의 경계

- 현재 `COMMITTED` 상태이며 성공 Run과 통과 Evidence가 있는 채택 Artifact 최대 2개. 원문 해시를 재확인하고 각 2,000자 발췌 및 잘림 여부를 기록한다.
- 실패 Run 최대 3개, 실패 원인 각 1,200자. 실패 산출물은 채택 결과로 재사용하지 않는다.
- 사용자 검토 이벤트 최대 3개, 의견 각 1,200자. 사람의 판단을 독립적 사실 검증으로 승격하지 않는다.
- 다른 태스크/다른 방법 버전, 오래된 Work, 평가 전용 Work 결과는 채택 결과 기억에서 제외한다. 입력을 갱신해도 같은 방법 버전의 과거 기억은 남으므로 현재 자료와 구분해야 한다.
- 고정 평가용 Work는 운영 기억을 전혀 받지 않는다. 평가 양쪽에 동일한 빈 기억을 제공하고 평가 결과·정답·운영 피드백의 누출을 막는다.
- 이전 실행의 스냅샷은 새 결과/검토로 바뀌지 않는다. 업데이트/삭제를 DB trigger로 금지한다. 이전 마이그레이션의 Run에는 기억이 없을 수 있다.

## 확인 위치

| 요구사항 | 구현 | 실행 확인 |
|---|---|---|
| 영속 기억과 provenance | `backend/app/work/task_memory.py`, `backend/migrations/023_task_memory.sql` | `GET /api/tasks/{task_id}/memory` |
| 원자적 claim과 기억 고정 | `backend/app/work/service.py` | `GET /api/runs/{run_id}`의 `task_memory`, `run.memory_pinned` 이벤트 |
| 기억의 실제 모델 입력 및 검증 | `backend/app/workloads/text_workflow.py` | Run context의 `task_memory`, 구조 검사 `task_memory=true` |
| 제한된 자동 복구 | `backend/app/worker.py`, `Kernel.fail` | 실패 Run 보존, `run.retry_scheduled`, 동일 Work의 새 attempt |
| 사용자 작업실 | `frontend/src/TaskWorkspace.vue`, `App.vue` | 첫 화면의 태스크·기억·이어가기·상태/근거 보기 |

텍스트 prompt는 `provided-text-step-v2`, 구조 검증은 `provided-text-structure-v4`이며 연결/결합 검증도 각각 v2로 구분한다. 세 데모에는 공통 기억 스냅샷이 생성되지만, 새 기억을 모델 입력으로 사용하는 범위는 사용자 텍스트 업무다. 기존 데모의 결정론적 계산·검증을 기억으로 변경하지 않는다.

## 검증 명령

사용자 데이터베이스에서 테스트를 실행하면 안 된다. 별도 PostgreSQL DB URL을 `WORKOS_TEST_DATABASE_URL`에 지정한다. 설치된 전역 pytest 플러그인의 외부 초기화 대기를 피하려면 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`을 사용한다.

```powershell
python -m pytest -x -q --tb=short tests/test_task_memory.py
python -m pytest -x -q --tb=short tests
npm --prefix frontend run build
```

`tests/test_task_memory.py`는 보완 의견의 실제 모델 요청 전달(테스트용 응답), 과거 스냅샷 불변성, 채택 결과 재사용, stale 제외, 태스크·버전 격리, 평가 양쪽 기억 격리, 동일 Work 자동 재시도 및 예산 소진을 검사한다. 테스트용 모델과 MOCK 결과는 실제 모델의 품질 향상을 입증하지 않는다.

## 남은 범위

범용 도구 선택·임의 업무 수행, 의미적 품질 자동 보증, 기억에서 방법 변경안을 자동 생성·적용하는 기능은 없다. LIVE 모델에 전달되는 정보는 현재 제공 텍스트·선행 결과뿐 아니라 같은 버전의 과거 결과 발췌·실패·검토 의견도 포함한다. 운영자가 등록한 텍스트의 전송 범위를 검토해야 한다. 기존 결과와 검토는 사용자 업무 데이터이며 별도의 외부 메모리 서비스로 전송하지 않는다.
