# Work OS 작업 상태

## 2026-11-01 추가 진행 — 고정 사례 기반 업무 방식 비교

상태: **부분 구현**. 사람이 고정한 같은 사례를 현재 업무 구조와 최신 후보 구조로 각각 재실행해 출력, 문구 검사, 실행 시간과 토큰을 비교한다. 후보의 목표·기대 결과·평가 기준을 그대로 유지하고 단계 ID도 같아야 한다. 후보는 자동 적용되지 않는다.

- `backend/migrations/015_text_experiments.sql`, `backend/app/evaluation/text_experiments.py`, `text_runtime.py`, `text_regression.py`: 평가셋과 기존 계약·후보 계획의 해시를 고정하고 사례별 두 WorkItem을 생성한다. 실행 입력·모델·산출물·컨텍스트·구조 검증 근거를 재확인하고, 동일 LIVE 모델·실행 유효·퇴행 없음·1건 이상 개선을 모두 만족해야 문구 검사 PASS다. 최신 설계나 평가셋이 바뀌면 비교를 완료하지 않는다. 평가용 실행은 일반 실행 이력과 사람의 산출물 채택에서 제외한다.
- `backend/app/workloads/text_workflow.py`, `backend/app/work/service.py`, `backend/app/api/main.py`: 단계별 경과 시간과 토큰 및 모델 응답 ID, 평가 범위를 기록하고 결과 조회·완료 API를 제공한다. 실패한 평가 실행은 비교 완료 전 재시도할 수 있다.
- `frontend/src/TextRegression.vue`, `WorkDesignStudio.vue`, `App.vue`, `style.css`: 후보 편집, 평가 시작과 상태, 사례별 기존/후보 결과·검사·퇴행·실패 원인을 표시한다. 시작 전 LLM 호출과 입력 전송을 안내한다.
- 전용 PostgreSQL·Redis에서 `python -m pytest -q tests`: **63 passed, warning 1건**. 이후 근거 재검증 등 보강한 최신 코드에 `python -m pytest -q tests/test_text_experiments.py tests/test_text_regression.py`: **12 passed, warning 1건**. `frontend`에서 `npm run build` 성공. 전체 63개는 마지막 보강 이전에 실행했다.
- 격리된 DB에서 사용자 설정의 실제 `gpt-6-luna`로 고정 사례 1건을 양쪽 실행했다. 기존 1/1, 후보 1/1로 동점이며 결과는 **FAIL**이다. 실제 응답과 토큰·시간을 별도 테스트 UI에서 확인했다. 의미적 품질은 평가하지 않았다.
- 기존 Compose의 API·worker·UI를 다시 빌드해 반영했다. 015 테이블 존재, API의 DB·OpenSearch 연결과 UI HTTP 200을 확인했다. API는 기본 데이터 모드를 MOCK으로 표시한다.

한계: 현재 평가는 사용자가 적은 필수·금지 문구의 대소문자 무시 부분 문자열 검사이며 의미적 정확성이나 실질적 품질 향상을 보장하지 않는다. 사례는 실험당 최대 10건, 후보는 같은 단계 ID를 유지해야 한다. 금액은 계산하지 않는다. 평가 통과 후에도 같은 업무 버전에 대한 적용·기각·롤백은 아직 없다. 기존 세 데모의 업무별 커널 분리도 남았다. 015 down 마이그레이션은 실행하지 않았다.

다음 세 단계: ① 최신 평가와 불변 근거를 다시 확인하는 명시적 적용·기각·롤백을 같은 Task의 버전과 기존 Job에 연결한다. ② 기존 세 업무의 검증·실행·후속 DAG를 커널 밖으로 이관한다. ③ 전 경로 E2E, 스케줄 연속성 및 장애 복구를 검증한다.

## 2026-11-01 추가 진행 — 사람 피드백을 고정 평가 사례로 기록

상태: **부분 구현**. 텍스트 업무의 사람이 검토한 산출물에서 평가 사례를 만들고 선택한 사례를 불변 평가셋으로 고정할 수 있다. 기존 방법과 후보의 재실행·비교·승격은 아직 구현하지 않았다.

- `backend/migrations/014_text_regression.sql`, `backend/app/evaluation/text_regression.py`, `backend/app/api/main.py`: 사용자가 익명화해 직접 입력한 평가 텍스트, 기대 결과, 피드백과 필수/금지 문구를 사람 검토 Evidence 및 원본 Artifact·Run·컨텍스트 해시에 연결한다. 원본 입력이 익명 입력과 같다고 주장하지 않는다. 케이스와 평가셋 스냅샷·버전·해시는 수정/삭제 불가이며 생성 요청은 멱등 처리한다.
- 사람 검토와 구조 검증이 없는 결과, 다른 업무 결과, 변경된 원본·컨텍스트·검토 증거, 존재하지 않는 단계, 모순된 문구를 거부한다. 이메일·전화번호·주민등록번호로 보이는 일부 패턴을 차단하지만 전체 개인정보 제거를 보장하지 않는다. 사람의 직접 익명화가 필요하다. 문구 검사는 대소문자 무시 부분 문자열 기준이며 의미적 정확성 평가는 아니다.
- `frontend/src/TextRegression.vue`, `App.vue`, `style.css`: 사람 검토 기록이 있는 텍스트 결과에서 사례 입력·목록·선택·평가셋 고정과 해시를 보여준다. 아직 방법 비교·자동 개선이 수행되지 않았다고 명시한다.
- 전용 PostgreSQL에서 `python -m pytest -q tests/test_text_regression.py tests/test_registered_work.py tests/test_work_designs.py tests/test_kernel.py`: **19 passed, warning 1건**. 이후 증거 해시·문구 충돌 검사 강화 후 최신 `python -m pytest -q tests/test_text_regression.py`: **3 passed, warning 1건**. 미검토 결과·개인정보 패턴·다른 업무·중복/모순 문구·원본/검토 증거 변조 거부와 이력 불변성을 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 별도 DB·API·UI의 브라우저에서 가상 결과를 검토한 뒤 익명 사례 저장, 사례 선택 및 평가셋 v1 고정을 확인했다. 기존 Compose의 데이터 볼륨을 유지하며 API·UI를 갱신했고 014 정방향 마이그레이션과 DB·검색 연결을 확인했다. 기존 사용자 업무에 테스트 입력을 추가하지 않았다.

한계: 고정 사례는 평가 준비일 뿐 실제 평가·품질 향상을 뜻하지 않는다. 선택된 사례를 기존/후보 방법으로 같은 입력에서 실행하고 퇴행을 검사한 뒤 같은 Task의 새 버전을 명시적으로 적용/기각/롤백하는 기능은 남았다. 개인정보 패턴 검사는 완전하지 않으며 사용자가 평가에 적합한 비식별 자료를 작성해야 한다. 014 down 마이그레이션과 기존 세 데모 전체 E2E는 이번 변경에서 실행하지 않았다.

다음 세 단계: ① 고정 평가셋의 동일한 입력을 기존/후보 방법에 각각 실행하고 문구 검사의 사례별 개선·퇴행을 기록한다. ② 평가 통과와 최신 근거를 확인한 뒤 같은 Task의 버전으로 명시적 적용·기각·롤백한다. ③ 기존 세 업무의 검증·실행·후속 DAG를 커널 밖으로 이관하고 전 경로 E2E 및 장애 복구를 검증한다.

## 2026-11-01 추가 진행 — 검토한 구조의 텍스트 업무 등록·수행

상태: **부분 구현**. 저장된 최신 자연어 업무 초안을 사람이 제공한 고정 텍스트와 함께 TaskDefinition/Job으로 등록하고, 수동 또는 서울 시간 Cron으로 실행한다. 단계는 선행 관계 순서로 수행하며 선행 결과와 고정 원문만 다음 단계에 전달한다. 원문·구조와 해시, 모델·응답 ID, 단계 로그, Artifact/Evidence/Evaluation을 남긴다. 새 입력이나 새 설계 버전은 현재 별도 업무로 등록되며 기존 업무의 개선 승격은 아직 아니다.

- `backend/migrations/013_registered_work.sql`, `backend/app/work/register_design.py`, `backend/app/workloads/text_workflow.py`: 버전 고정 등록, 명시적 MOCK/LIVE_LLM, 텍스트 전용 DAG 실행과 구조 검증. 동일 설계 버전 재등록은 동일 설정에서 멱등이며 다른 입력·설정은 거부한다. MOCK 기본 초안의 미작성 기대 결과와 평가 기준은 등록을 거부한다. 외부 검색·시스템 조작은 지원하지 않는다.
- 형식·단계 순서·입력 해시 검증에 통과해도 모델 결과를 자동 채택하지 않는다. `WAITING_APPROVAL`에서 사람이 기대 결과와 평가 기준을 확인하고 의견과 함께 채택 또는 보완 요청을 기록한다. 기존 유효 산출물과 실패 이력을 보존하고 오래된 결과·해시 변조·중복 요청을 차단한다. 사람 판단은 사실 정확성의 독립 검증이 아니다.
- Cron 작업은 화면에서 중지/재개할 수 있다. 중지 시 새 예약·수동 요청이 차단되고 이미 생성된 작업은 유지된다. 재개 시 다음 일정부터 실행한다. 스케줄러가 중지 전 읽은 오래된 cursor로 작업을 만들지 않도록 확인한다. LIVE_LLM 반복 실행에는 호출 비용이 발생한다.
- `frontend/src/WorkDesignStudio.vue`, `App.vue`, `RunHistory.vue`: 초안의 입력·모드·일정 등록, 사용자 업무 목록과 이력, 단계별 결과·사람 검토·일정 중지/재개를 제공한다. 다른 업무의 단순 수치 변화나 형식 검사를 업무 품질 향상으로 표시하지 않는다.
- 격리된 PostgreSQL·Redis·OpenSearch의 전체 `python -m pytest -q tests`: **52 passed, warning 1건**. 이후 등록 계약·일정 중지/재개·변조 차단과 10개 서로 다른 입력/단계 구조를 추가한 최신 `python -m pytest -q tests/test_registered_work.py tests/test_scheduler.py`: **5 passed, warning 1건**. 최초 전체 테스트는 기존 OpenSearch 서비스 부재로 5건 실패했고, 별도 OpenSearch에서 재실행해 통과했다. `npm run build`, `docker compose config --quiet` 성공.
- 별도 DB에서 실제 `gpt-6-luna`로 등록된 텍스트 업무를 실행해 `WAITING_APPROVAL`, `LIVE_LLM`, `NOT_PERFORMED`를 확인했다. 별도 API·워커 및 브라우저에서 가상 업무 등록, 주기 중지/재개, 단계 실행, 기대 결과·기준 표시와 사람 검토 후 채택을 확인했다. 이후 중단되어 있던 기존 Compose를 데이터 볼륨을 유지하며 다시 시작했다. 기존 DB에 013 정방향 마이그레이션이 적용되고 API·워커·스케줄러와 검색 연결을 확인했다. 사용자 업무에 테스트 요청이나 검토를 추가하지 않았다.

한계: 자료는 등록 시 제공한 고정 텍스트이며 검색·외부 연계와 자동 갱신은 없다. 단계 내용의 의미적 정확성은 자동 검증하지 않으며 사용자 검토를 기다린다. 기존 TASK-001/002/003의 업무별 분기가 커널에 남아 있다. 사람의 피드백을 고정 평가셋에 반영하는 범용 Champion/Challenger 비교와 동일 업무의 버전 적용·기각·롤백, 기존 세 데모 전체 E2E는 이번 변경에서 완료/재실행하지 않았다. 동일 Run 장애 자동 재개는 아직 없다. 013 down 마이그레이션은 실행 검증하지 않았다.

다음 세 단계: ① 실행 결과에 대한 피드백을 버전 고정된 평가 사례·기대 결과로 축적하고, 같은 입력의 후보·기존 방법을 비교하는 범용 평가를 구현한다. ② 명시적 적용·기각·롤백을 같은 Task의 버전으로 연결하고 stale 평가와 퇴행을 차단한다. ③ 기존 세 업무의 검증·실행·후속 DAG를 커널 밖으로 이관하고 전 경로 E2E 및 장애 복구를 검증한다.

## 2026-11-01 추가 진행 — 자연어 업무 구조 초안

상태: **부분 구현**. 사람의 자연어 설명을 LIVE_LLM 또는 명시적 MOCK으로 업무 목표·기대 결과·평가 기준·단계·선행 관계·확인 질문의 초안으로 변환한다. 화면에서 직접 수정하거나 저장된 초안에 자연어 변경 요청을 보내 새 버전을 만들 수 있다. `work_design_revisions`는 과거 구조·변경 요청·모델 식별자를 보존하고 수정·삭제를 막는다. DAG 순환·미등록 의존성, 중복 ID, 낡은 버전의 덮어쓰기를 거부한다.

- `backend/migrations/012_work_design.sql`, `backend/app/work/designs.py`, `backend/app/api/main.py`: 초안·불변 버전·생성/직접 수정/자연어 피드백 API. API 컨테이너가 선택적 `.env.apikey`를 읽도록 구성했으며 키는 저장·출력하지 않는다. OpenAI Structured Outputs로 구조를 생성하고 애플리케이션에서 별도로 그래프를 검증한다.
- `frontend/src/WorkDesignStudio.vue`, `frontend/src/App.vue`, `frontend/src/style.css`: 자연어 입력과 초안 생성, 단계·선행 관계 시각화 및 수정, 단계 추가/삭제, 평가 기준, 변경 요청, 버전 이력. 화면은 이 결과를 실행 전 초안으로 명시한다.
- 전용 PostgreSQL의 `python -m pytest -q tests/test_work_designs.py tests/test_api.py tests/test_kernel.py`: **14 passed, warning 1건**. 초안 저장, 직접 수정, 오래된 버전 충돌, DAG 순환 거부, 자연어 피드백 버전 및 원본 보존을 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 별도 테스트 DB의 실제 API에서 사용자 제공 모델 설정으로 자연어 생성과 자연어 피드백을 호출해 `LIVE_LLM → HUMAN_FEEDBACK_LLM`, revision 2, `gpt-6-luna`를 확인했다. 실제 모델 결과의 의미적 정확성은 평가하지 않았다. 기존 Compose에 새 API와 UI를 반영하고 브라우저에서 입력 및 초안 빈 화면을 확인했다. 이후 버전 이력 화면 보강은 빌드로만 확인했다.

한계: **새 초안은 TaskDefinition/Job으로 승격되거나 지속 실행되지 않는다.** 자연어 피드백은 설계 초안만 수정하며 실행 결과를 재평가하거나 검증된 성능 개선을 보장하지 않는다. 기존 업무별 커널 분기, 범용 DAG 실행·검증기·실행 후 개선 평가, 자동 반복 실행은 미완료다. 기존 세 데모의 전체 E2E는 이번 변경에서 재실행하지 않았다.

다음 세 단계: ① 사람이 확인한 구조를 버전 고정된 Task/Job과 실행 가능한 노드·출처·검증 계약으로 등록하고, 지원하지 않는 능력은 명시적으로 차단한다. ② 공통 DAG 실행·이력·검증과 안전한 정기 수행을 연결하며 기존 세 업무의 특수 분기를 모듈로 이관한다. ③ 실행별 사람 피드백을 고정 평가셋과 동일 입력 Champion/Challenger 실험으로 연결해 명시적 적용·기각·롤백을 검증한다.

## 2026-11-01 추가 진행 — 업무별 실행 이력과 결과 비교 화면

- 대시보드 상단의 `무엇이 달라졌나요?`에서 세 Task의 최상위 Run 최대 100건을 조회하고, 성공·검증 실패·실행 실패와 결과 요약을 표시한다. 이전 실행을 펼치거나 선택한 시도의 결과·근거·단계로 이동할 수 있다. 후속 DAG Run은 별도 정책 상세에서 확인한다.
- 검증 통과한 두 실행을 선택해 금융 지표, 정책 변경 조항 수, VOC 범주별 건수를 비교한다. 데이터 증감을 품질 향상으로 판단하지 않는다. TASK-003은 별도의 고정 평가 결과에서 Champion/Challenger 점수·개선·퇴행·판정을 표시하고 근거 화면으로 연결한다. 모든 결과는 가상 사례의 범위를 명시한다.
- `frontend/src/RunHistory.vue`, `frontend/src/App.vue`, `frontend/src/style.css`, `backend/app/api/main.py`, `tests/test_market.py`, `tests/test_policy.py`를 변경했다. 비금융 Task에 금융 전용 비교 API를 호출하면 422를 반환한다.
- 전용 PostgreSQL에서 `python -m pytest -q tests/test_market.py tests/test_api.py tests/test_policy.py tests/test_voc.py`: **15 passed, warning 1건**. 이후 정책 후속 Run 제외 검사를 추가하고 `python -m pytest -q tests/test_policy.py`: **5 passed, warning 1건**. `npm run build`, `docker compose config --quiet` 성공.
- 기존 Compose 데이터는 유지한 채 API·UI 이미지를 갱신하고 브라우저에서 금융 비교, VOC 평가 요약 및 과거 실행 상세, 정책 실행 기록과 선택한 정책 조항·출처를 확인했다. 별도 E2E 전체 시나리오는 이번 변경에서 재실행하지 않았다.

한계: 이력은 최상위 Run 최근 100건이며, 데이터 변화와 결정론적 검사 수는 의미적 품질 점수가 아니다. 이전 업무 확장 경로는 여전히 부분 구현이다.

다음 세 단계: ① 기존 세 업무의 실행·검증·정책 후속 DAG를 확장 모듈로 분리한다. ② 신규 업무의 컨텍스트·체크포인트·DAG와 검증기 버전 고정을 공통 계약으로 지원한다. ③ 복수 신규 업무의 커널 무수정 E2E와 기존 세 데모 회귀를 검증한다.

## 2026-11-01 추가 진행 — 신규 업무 실행·검증 확장 경로

상태: **부분 구현**. 신규 업무 실행기와 독립 검증기를 신뢰된 배포 모듈로 등록하고 공통 커널에서 검증·발행하는 경로를 추가했다. 기존 세 데모의 업무별 분기와 정책 DAG는 여전히 커널·런타임에 남아 있어, 모든 종류의 신규 업무를 완전히 일반화한 것은 아니다.

- `backend/app/workloads/registry.py`, `example_total.py`: 업무 ID/버전 실행기와 acceptance kind별 검증기 등록. 기존 업무/검증기 이름과 중복 등록을 거부하고, 명시적으로 구성된 신뢰 모듈만 API와 워커 시작 시 로드한다. 플러그인은 프로세스 내부 코드이며 샌드박스가 아니다.
- `backend/app/work/service.py`, `worker.py`, `api/main.py`, `docker-compose.yml`: 새 확장 검증기는 기존 lease·트랜잭션·상태 전이를 통해 Artifact/Evidence/Evaluation을 기록한다. 미등록 검증기와 잘못된 검증기 반환을 거부하고, 검증 실패는 결과를 채택하지 않는다. 새 확장 실행기는 현재 Graph 체크포인트와 범용 DAG를 자동으로 제공하지 않는다.
- `tests/test_workload_extension.py`에서 네 번째 예시 업무의 실행·검증·변조 거부와 미등록 검증기 거부를 확인하고 HTTP API로 실행·결과 조회도 확인했다. 설정·계약·한계는 `docs/WORKLOAD_EXTENSIONS.md` 참조.
- 전용 PostgreSQL·Redis와 로컬 OpenSearch에서 전체 `python -m pytest -q tests`: **47 passed, warning 1건**. 그 후 HTTP API 확인을 추가한 최신 `python -m pytest -q tests/test_workload_extension.py`: **2 passed, warning 1건**. 검증용 DB는 별도 컨테이너였다.

다음 세 단계: ① 기존 세 업무의 실행·검증·정책 후속 DAG를 확장 모듈로 분리하고 기존 데이터·동작을 유지한다. ② 신규 업무의 컨텍스트·체크포인트·의존성 DAG 및 검증기 버전 고정을 공통 계약으로 지원한다. ③ 복수의 서로 다른 신규 업무를 커널 수정 없이 별도 프로세스에서 등록·실행하는 E2E와 기존 세 데모 회귀를 검증한다.

## 2026-11-01 추가 진행 — 작업 결과 보고서

현재 단계: **P0 커널과 P1 세 가지 가상 시연 경로 구현**, P2 실제 연계·운영 준비 미완료. 사용자 요청에 따라 `docs/WORK_REPORT.md`에 현재 구현과 세 시나리오, 코드·테스트 위치, 실제 실행된 검증, 시연 방법, 한계와 다음 작업을 정리했다. 보고서 기준 구현 커밋은 `2110c93`이다.

검증: 보고서의 Markdown 상대 링크 38개를 Python으로 확인해 누락 0건. 기존 문서·코드·Git 이력을 대조했으며 이 문서 변경에 대해 애플리케이션 테스트를 다시 실행하지 않았다. 이전 전체 테스트 결과는 45 passed, warning 1건이다. 링크 검사 스크립트의 첫 실행은 Python f-string 문법 오류로 실패했고 수정한 다음 실행이 성공했다.

제약과 다음 단계는 바로 아래 최신 구현 기록과 같다: ① 동일 Run의 안전한 자동 장애 재개를 구현·검증한다. ② 실제 읽기 전용 원천과 변경 감지·복구를 연결한다. ③ 인증·운영 관측성과 장시간 실행·동시성 부하를 검증한다.

## 2026-11-01 추가 진행 — 시연 화면과 배포 준비

현재 단계: **P0 커널 및 P1 세 가지 가상 시연 경로 구현**. 실제 시스템 연계와 운영 수준 기능을 포함한 `init.md` 전체 계약은 미완료다.

- `/api/tasks`가 각 Task의 최상위 Work에 대한 최근 Run 최대 5건을 실제 DB에서 조회해 실행 상태, 검사 판정·버전, 결정론적 검사 통과 수와 Work의 현재 채택 여부를 반환한다. 결과가 없는 실행이나 실패한 산출물을 채택으로 표시하지 않는다. 같은 Work가 아닌 다른 Work의 채택 결과도 각 Work의 채택 이력이다.
- 대시보드 카드에 해당 이력과 PostgreSQL/OpenSearch 상태를 표시한다. 서로 다른 검증 버전의 숫자는 직접 비교할 수 없고 의미적 품질 점수도 아니라는 범위를 화면에 명시했다. TASK-001의 선택 가능한 가상 지표 비교, TASK-003의 고정 사례 Champion/Challenger 비교는 각각 별도 화면에서 제공한다.
- `backend/app/api/main.py`, `frontend/src/App.vue`, `frontend/src/style.css`, `tests/test_api.py`, `tests/test_market.py`를 변경했다. `README.md`의 오래된 LangGraph·정책 인덱싱 설명을 현재 구현에 맞추고 실제 장애 복구 한계를 기록했다.
- 전용 PostgreSQL·Redis와 로컬 OpenSearch를 사용한 `python -m pytest -q tests`: **45 passed, warning 1건**, 115.08초. 실패한 실행, 실패한 검증, 복구 후 성공과 검사 통과 수를 확인했다. 처음 `localhost`로 시작한 전체 테스트는 IPv6 연결 지연 때문에 중단했으며 성공으로 계산하지 않았다. 최종 실행은 세 테스트 주소에 `127.0.0.1`을 사용했다.
- `npm run build` 성공. `docker compose -p workos-p0 up -d --build api ui` 후 브라우저에서 세 카드의 판정·이전 버전·검사 수·연결 상태를 확인했다. 기존 데이터와 사용자 검토 상태는 변경하지 않았다. 별도 빈 Compose의 세 흐름 전체 E2E 및 실제 키를 이용한 LIVE_LLM 호출 검증은 이전 이 절 아래 기록을 따른다.

한계: 시장·정책·VOC와 평가 사례는 가상이다. LIVE_LLM은 TASK-001 해석에만 사용하며 의미 정확성을 검증하지 않는다. 체크포인트는 영속화하고 내부 동일 Run 재개 경로를 시험했지만 워커 장애 후 lease 만료 시에는 기존 Run을 실패로 보존하고 새 시도를 만든다. 같은 Run의 자동 장애 재개, 실제 사내 데이터 연결과 운영 보안은 미완료다.

다음 세 단계: ① 워커 소유권과 lease fencing을 보강하고 중단된 동일 Run의 안전한 자동 재개를 구현·검증한다. ② 실제 읽기 전용 원천을 별도 설정 가능한 어댑터로 연결하고 출처 변경 감지와 복구를 검증한다. ③ 인증·운영 관측성과 장시간 실행 및 동시성 부하 테스트를 추가한다.

## 2026-10-31 추가 진행 — VOC 서비스 문서 출처와 Git 준비

현재 세 가상 시연 흐름은 실제 API·워커와 대시보드에서 재현 가능하며 전체 `init.md` 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- TASK-003 v4 / `voc_report_v3`를 추가해 가상 결제·배송·고객지원 문서의 버전 ID를 각 추천에 연결한다. v2/v3 Work와 과거 Artifact는 보존하고 계속 처리한다. 새 Artifact 스키마 버전은 3이다.
- Run 컨텍스트에 읽기 전용 SQL 질의·매개변수, VOC 원본, 서비스 문서의 원본·검색 결과와 해시, 검색 질의를 기록한다. `voc-validator-v3`는 문서 ID·버전·해시와 추천 범주별 인용, 검색 결과와 원본의 현재성, VOC 레코드의 현재성을 검사한다. 누락·변조나 원본 변경 시 기존 유효 결과를 보존하고 새 결과는 채택하지 않는다. 검증 시점의 재조회이며 외부 데이터가 이후 바뀌지 않는다는 보장은 아니다.
- 데모 인덱스 준비 상태가 가상 문서 6건을 확인하고, Compose 워커는 검색 시드 완료 후 시작한다. 대시보드는 가상 문서와 `OPENSEARCH`/`FIXTURE_DB`를 표시하며 추천의 의미적 적합성은 검증하지 않았다고 명시한다.
- 전용 PostgreSQL·Redis·OpenSearch 전체 `python -m pytest -q tests`: **44 passed, warning 1건**. 이후 질의·매개변수 기록과 원본 레코드 재검사를 보강한 최신 코드에서 `python -m pytest -q tests/test_voc_sources.py tests/test_voc_history.py tests/test_voc.py tests/test_search_readiness.py`: **14 passed, warning 1건**. 문서 누락·인용 변조·인덱스/DB 변경·원본 레코드 추가·복구를 검증했다.
- 별도 빈 Compose의 세 시나리오 E2E를 통과했고, 최종 코드로 별도 환경을 갱신한 후 실제 워커 Run에서 TASK-003 v4, `OPENSEARCH`, 읽기 전용 PostgreSQL, 문서 3건, 원본과 검색 현재성 통과, SQL 기록을 확인했다. 브라우저에서 문서 ID와 의미 검증 한계를 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 이 작업 폴더에 Git을 초기화하고 원격을 사용자가 지정한 공개 저장소 `leemg1476/workOS`로 연결했다. 제품 명세, 백엔드, 대시보드·데모 환경을 로컬 커밋으로 구분했다. `.env.apikey`, 캐시와 빌드 산출물은 `.gitignore`로 제외하고 자격증명 패턴을 검사했다. 아직 원격에 푸시하지 않았다.

제약: 모든 자료는 가상이다. 문서가 존재하고 내용·버전·해시가 일치한다는 검사는 추천의 의미적 타당성이나 실제 고객 유용성을 보장하지 않는다. 별도 데이터베이스와 검색 시스템 간 검사는 분산 트랜잭션이 아니며, 워커 종료 후 같은 Graph Run의 자동 재개는 미완료다.

다음 세 단계: ① 전체 `init.md` 데모·운영 계약을 실제 동작과 대조해 남은 필수 항목을 처리한다. ② 품질 변화와 최근 결과를 Work 화면에서 쉽게 확인하도록 보강한다. ③ 워커 소유권·lease fencing과 체크포인트 복구를 구현하고 완료 후 변경을 단위별 커밋해 지정 원격에 푸시한다.

## 2026-10-31 추가 진행 — VOC 이전 추천 비교와 계약 버전

현재 세 가상 시연 흐름은 실제 API·워커와 대시보드에서 재현 가능하며 전체 `init.md` 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- TASK-003 v3과 `voc_report_v2` 출력 계약을 추가했다. 기존 v2 계약과 그 Work/Run은 보존하며 v2로 이미 생성된 작업도 계속 실행할 수 있다. 새 Job은 v3을 사용하고 새 VOC Artifact의 스키마 버전은 2다.
- Run 컨텍스트를 만들 때 이미 검증에 성공한 가장 최근 VOC 보고서와 해당 Run·Artifact·Evidence ID, 해시, 비교 시점을 고정한다. 추천별로 `이전 보고서 없음`, `동일 범주`, `새 범주`를 기록한다. 결정론적 범주 ID 비교이며 의미상 중복·사업상 유용성에 대한 판단이 아니다.
- `voc-validator-v2`는 고정된 이전 산출물의 해시·검증 성공 이력, 보고서에 적힌 참조, 비교 시점·범위, 범주별 결과를 다시 확인한다. 잘못된 비교나 이전 산출물 변조 시 새 결과를 채택하지 않으며 실패 Run과 기존 유효 보고서를 보존한다. 비교는 Run 컨텍스트 작성 시점의 이력 스냅샷이다.
- `backend/app/domain/contracts.py`, `backend/app/work/voc.py`, `backend/app/work/service.py`, `backend/app/work/seed.py`, `backend/app/runtime/graph.py`, `backend/app/worker.py`, Artifact 어댑터, 대시보드와 테스트를 변경했다. 새 테이블은 필요하지 않아 기존 TaskDefinition·Artifact·Evidence 구조를 사용한다.
- 전용 PostgreSQL·Redis·OpenSearch에서 전체 `python -m pytest -q tests`: **42 passed, warning 1건**. 이후 이전 보고서의 Evaluation ID/검증기 버전을 고정하고 미검증 의미 중복 주장을 출력에서 거부하도록 강화한 뒤 `python -m pytest -q tests/test_voc_history.py tests/test_voc.py`: **10 passed, warning 1건**. 첫 보고서, 반복·새 범주, 조작된 비교와 이전 해시, 실패 후 재시도, 기존 v2 Work와 새 v3 연결을 검증했다.
- 별도 빈 Compose의 `python -u tests/e2e_stack.py`가 세 시나리오를 통과하며 첫 VOC 보고서의 이전 이력 없음과 다음 보고서들의 이전 산출물 연결·동일 범주를 확인했다. 브라우저에서 `의미상 중복 여부는 미판단`, 이전 보고서·Evidence 링크, `voc-validator-v2`를 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 강화한 최신 코드로 빈 별도 Compose를 다시 생성해 동일 E2E를 통과했고, 기존 데모 환경은 `docker compose -p workos-p0 up -d --build` 후 TASK-003 v3이 활성화된 것을 확인했다. 기존 Work와 이력은 삭제하지 않았다.

제약: 같은 범주라는 표시는 추천 문장의 의미적 유사성이나 실제 중복 여부를 검증하지 않는다. 실제 고객 데이터와 비공개 평가셋은 없고, 별도의 제품/서비스 문서 검색과 워커 종료 후 같은 Graph Run 자동 재개도 미완료다.

다음 세 단계: ① VOC 입력에 가상 상품·서비스 문서의 버전과 검색 근거를 연결한다. ② 동시 실행과 출처·이전 이력 변경에 대한 검증 경계를 강화한다. ③ 워커 소유권·lease fencing과 체크포인트 복구를 구현한다.

## 2026-10-31 추가 진행 — 명시적 담당자 검토 기록

현재 세 가상 시연 흐름은 실제 API·워커와 대시보드에서 재현 가능하며 전체 `init.md` 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- 네 번째 후속 Work에서 사용자가 먼저 현재 검토 대상을 불러온 후 `REVIEWED` 또는 `CHANGES_REQUESTED`와 판단 근거를 명시할 수 있다. 가상 문서 ID/해시, 현재 VOC 방법·버전, 평가셋 해시, 네 선행 산출물·Evidence·검증기 버전을 고정하고 대상 해시가 변경되면 이전 화면의 제출을 거부한다. 검색 문서가 없어지거나 선행 Work가 미완료인 경우도 기록을 거부한다.
- 사람의 판단은 `HUMAN` Run, 산출물, 검토 맥락과 `human-review-record-verifier-v1` Evidence/Evaluation, 사용자 이벤트로 같은 DB 트랜잭션에 남는다. 검증 범위는 기록과 연결의 무결성이며 판단의 정당성이나 정책 승인이 아니다. 같은 요청은 한 번만 기록되고 다른 내용으로 멱등 키를 재사용할 수 없다.
- 기록된 검토 Work는 `COMMITTED`이나 이는 **검토 기록 완료**만 뜻한다. 두 선택 모두 실제 정책·Task 설정을 바꾸지 않고 영향받은 Task는 계속 `REVIEW_REQUIRED`다. 사용자가 재검토를 열거나 부모 산출물·VOC 방법이 바뀌면 이전 결정은 보존된 과거 기록으로 표시되고 선행 검사가 통과한 뒤 다시 `WAITING_APPROVAL`로 돌아간다. 일반 Work 재검증으로 사람의 검토 Work를 실행 큐에 넣을 수 없다.
- `backend/app/work/service.py`, `backend/app/domain/contracts.py`, `backend/app/api/main.py`, `frontend/src/App.vue`, 정책 통합 테스트와 E2E를 변경했다. 기존 PostgreSQL Work/Run/Artifact/Evidence/Event 구조를 재사용했으며 신규 테이블이나 외부 승인 시스템은 없다.
- 전용 PostgreSQL·Redis·OpenSearch의 `python -m pytest -q tests/test_policy.py tests/test_policy_index.py tests/test_policy_regression.py`: **9 passed, warning 1건**. 선행 미완료·검색 문서 변경·오래된 맥락 거부, 중복 요청과 동시 중복 요청, 기록 재열기, 모델 변경 시 이전 판단 stale 처리를 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 전체 `python -m pytest -q tests`: **38 passed, warning 1건**. 이후 검토 시 변경 전·후 문서 양쪽의 검색 결과와 원본 해시를 확인하도록 강화했고, 최신 코드의 `python -m pytest -q tests/test_policy_regression.py`는 **1 passed, warning 1건**이다. 이 테스트는 두 버전 중 하나라도 검색에서 사라지면 검토 결정을 거부하는 것을 확인했다.
- 빈 별도 Compose의 `python -u tests/e2e_stack.py`는 세 시나리오, 명시적 검토/보완 요청, VOC 방법 적용·롤백과 부모 분석 재검증 후 기존 판단의 재검토 전환을 통과했다. 별도 환경 브라우저에서 근거 불러오기, 판단 입력, 검토 기록과 정책 미적용 표시를 확인했다. 검토 버튼은 실제 기존 데모 상태에서 대신 누르지 않았다.
- 최신 코드를 반영한 별도 빈 Compose에서 동일 E2E를 다시 통과했다. 기존 프로젝트도 `docker compose -p workos-p0 up -d --build`로 갱신했으며 읽기 전용 API 확인 결과 문서 `POLICY-VOC@1 → @2`, Evidence 4건, 검토 `WAITING_APPROVAL`, 기존 결정 0건이다. 검증용 별도 프로젝트와 테스트 컨테이너만 정리했다.

제약: 단일 사용자 로컬 데모이므로 검토자 신원 인증은 없고, 검색 데이터의 확인은 결정 시점에 이뤄진다. 가상 VOC 회귀는 정책의 의미·준법 여부를 검증하지 않는다. 정책 실제 적용 기능과 워커 종료 후 같은 Graph Run의 자동 재개도 없다.

다음 세 단계: ① 사용자 검토 후 별도 출처 재검증과 정책 적용 여부의 명시적 처리 범위를 제품 계약과 맞춰 검토한다. ② 사람의 판단과 이력이 변경된 근거와 연결되는 전체 브라우저 경로를 지속 회귀 테스트한다. ③ 워커 소유권·lease fencing과 체크포인트 복구를 구현한다.

## 2026-10-31 추가 진행 — 정책 후속 VOC 회귀 검사

현재 세 가상 시연 흐름은 실제 API·워커에서 재현 가능하며 전체 `init.md` 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- 정책 인덱스 후속 Work가 통과하면 세 번째 Work를 큐에 넣는다. 가상 정책 v2의 검색 결과와 검증된 의존 산출물, 현재 VOC 분류 방법·상태 버전, `champion-v1` 기준, 고정 평가셋 24건 및 별도 사례 6건의 해시를 Run 컨텍스트에 고정한다. 각 사례에서 두 방법을 비교하고 기준에서 맞힌 사례를 현재 방법이 틀리는 퇴행이 없어야 한다. 독립 검증기는 검색 및 의존성의 현재성, 입력 해시, 사례별 결과 재계산과 퇴행 여부를 확인한다.
- `backend/app/evaluation/policy_regression.py`, `backend/app/work/service.py`, `backend/app/evaluation/improvements.py`와 Worker/API/UI에 `policy-regression-verifier-v1`을 연결했다. VOC 방법의 명시적 적용·롤백 시 기존 검사를 stale로 만들고 새 Run을 예약한다. 진행 중이던 이전 방법의 Run은 실패 이력으로 보존하고 늦은 결과 제출을 거부한다. 워커 기동 시 기존에 대기하던 검사를 선행 조건 확인 후 복구한다.
- 전용 PostgreSQL·Redis·OpenSearch에서 `python -m pytest -q tests/test_policy.py tests/test_policy_index.py tests/test_policy_regression.py`: **9 passed, warning 1건**. 알려진 퇴행 차단, 보고서 숫자 변조 거부, 재시도, 실행 중 방법 적용과 롤백·이전 Run 차단을 검증했다. `npm run build`, `docker compose config --quiet` 성공.
- 전체 `python -m pytest -q tests`: **38 passed, warning 1건**. 전체 테스트 시작 후 검색 문서 소실 사례를 추가해 `python -m pytest -q tests/test_policy_regression.py`를 별도로 재실행했으며 **1 passed, warning 1건**이다. 준비 후 문서가 검색에서 사라지면 발행이 거부되고 복구 후 새 Run이 성공했다.
- 별도 빈 Compose의 `python -u tests/e2e_stack.py`에서 세 시나리오와 정책 후속 세 단계가 통과했다. 다시 초기화한 별도 환경에서는 개선안 적용과 롤백 때 실제 워커가 회귀 Work를 각각 새 방법으로 재실행하는 것도 확인했다. 브라우저에서 고정 사례 24건 기준 18/현재 18, 별도 사례 6건 기준 3/현재 3, 퇴행 0건 및 의미 검증 범위 제한을 확인했다. 기존 프로젝트를 `docker compose -p workos-p0 up -d --build`로 갱신한 후 세 번째 Work `COMMITTED` / Run `SUCCEEDED`를 API로 확인했다.

제약: 이 검사는 가상의 고정 VOC 분류 사례를 고정 기준 방법과 비교한다. 규정 변경 전후의 인과적 영향, 정책 해석의 의미적 정확성 또는 실제 법규 준수를 검증하지 않는다. 담당자 검토 Work는 `WAITING_APPROVAL`이며 메모는 승인이나 검토 완료가 아니다. 워커 종료 후 같은 Graph Run을 자동으로 이어가는 기능도 미완료다.

다음 세 단계: ① 선행 Evidence와 대상 버전을 고정해 사용자의 검토 완료/반려를 기록하고 새 정책을 자동 적용하지 않는다. ② 출처·방법 변경 시 검토 결정이 오래된 것으로 처리되는지 검증한다. ③ 워커 소유권과 lease fencing을 보강해 안전한 체크포인트 복구를 구현한다.

## 2026-10-31 추가 진행 — 가상 정책 인덱스 갱신 후속 Work

현재 세 가상 시연 흐름은 실제 API·워커에서 재현 가능하며 전체 `init.md` 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- 첫 후속 검사가 통과하면 두 번째 Work `검색 인덱스 갱신`을 큐에 넣는다. 검증된 선행 산출물과 규정 v2의 원본·해시·인덱스 설정을 Run 컨텍스트에 고정하고, 로컬 데모 OpenSearch에 `POLICY-VOC@2`를 생성한다. `op_type=create`로 같은 버전을 덮어쓰지 않으며, 이미 존재하면 내용 일치 시에만 재사용한다. 원격 주소, 인증 정보가 포함된 주소, 데모/테스트 이외 인덱스, MOCK 이외 환경은 쓰기를 거부한다.
- `policy-index-verifier-v1`은 별도 검색으로 v2의 ID·내용·해시, 기존 v1 보존, 원천 문서 및 고정된 선행 Work를 검사하고 Evidence/Evaluation에 남긴다. 일치하지 않으면 산출물을 채택하지 않는다. 장애 시 같은 Work에 새 Run으로 재시도하고 이전 실패를 보존한다. 부모 재검증 시 두 후속 Work를 stale로 전파하고 선행 결과를 다시 고정한다. 워커 시작 시 기존에 대기하던 두 번째 작업도 조건을 확인해 복구한다.
- `backend/app/integrations/search.py`, `backend/app/work/service.py`, `backend/app/worker.py`, `backend/app/domain/contracts.py`, `backend/app/api/main.py`, `frontend/src/App.vue`, `tests/test_policy_index.py`와 E2E를 변경했다. UI는 실제 검사 결과와 실패 재시도를 표시한다. 인덱스 변경은 재구축 가능한 로컬 가상 검색 계층에만 적용하며 운영 규정이나 Task 설정은 변경하지 않는다.
- 전용 PostgreSQL·Redis 및 임시 OpenSearch 인덱스에서 `python -m pytest -q tests/test_policy.py tests/test_policy_index.py`: **7 passed, warning 1건**. 테스트는 v2 생성·동일 버전 재사용·충돌 덮어쓰기 거부, 변조 감지, 실패 후 새 Run, v1 보존 및 부모 재검증을 확인했다. `npm run build`, `docker compose config --quiet` 성공.
- 전체 `python -m pytest -q tests`: **36 passed, warning 1건**. 전체 테스트가 시작된 후 인덱스 영수증에 임의의 승인 주장 필드를 넣지 못하도록 계약을 강화했으며, 추가된 해당 계약 테스트는 별도로 **1 passed, warning 1건**이다.
- 별도 빈 Compose에서 `python -u tests/e2e_stack.py`로 세 시나리오, 후속 두 단계와 검토 메모, 개선안 적용·롤백을 통과했다. 브라우저에서 가상 v2 인덱스 `COMMITTED`, `policy-index-verifier-v1`, 이후 대기 단계를 확인했다. `docker compose -p workos-p0 up -d --build` 후 기존 프로젝트의 두 번째 후속 Work도 `COMMITTED` / Run `SUCCEEDED`임을 API로 확인했다.

제약: 인덱스 검증은 해당 Run 시점의 검색 결과에 대한 확인이다. 회귀 테스트 Work는 `BLOCKED`, 담당자 검토 Work는 `WAITING_APPROVAL`이며 사용자 메모는 승인이나 완료가 아니다. 모델의 의미 판단, 실제 정책 준수, 운영 데이터의 정확성을 보장하지 않는다. 체크포인트의 워커 종료 후 자동 재개도 미완료다.

다음 세 단계: ① 고정 VOC 평가셋을 사용하는 정책 영향 회귀 검사를 후속 Work에 연결한다. ② 근거와 선행 결과를 고정한 명시적인 사용자 검토 완료 경로를 설계한다. ③ 워커 소유권과 lease fencing을 보강해 안전한 체크포인트 복구를 구현한다.

## 2026-10-31 추가 진행 — 규정 후속 검사와 사람의 검토 메모

현재 세 가지 가상 시연 흐름을 실행할 수 있으며 전체 `init.md` 계약은 미완료다. 이전 기록보다 이 절이 최신이다.

- 정책 분석을 채택하면 첫 후속 Work `변경 해석 검증`을 자동으로 큐에 넣고 별도 Run에서 부모 분석의 원본 컨텍스트, 산출물 해시, Evidence 및 조항·출처·DAG를 재검사한다. `PolicyFollowupVerification` 계약, `policy-followup-verifier-v1` 결과와 Evaluation을 기록하고 검증 통과 시에만 COMMITTED다. 검사는 저장된 스냅샷에 대한 결정론적 재검사이며 의미적 검토는 `NOT_PERFORMED`다.
- 부모 분석을 재검증하면 첫 후속 결과를 STALE로 전파하고 새 산출물에 의존성을 다시 고정해 새 Run으로 검사한다. 실패나 위조된 결과는 발행되지 않으며 같은 Work의 새 Run으로 재시도한다. 기존에 생성된 BLOCKED 후속 작업은 워커 시작 시 검증된 부모가 있을 때만 중복 없이 큐에 복구한다.
- `POST /api/policy-work/{work_id}/review-notes`와 대시보드에 현재 검증된 분석에 대한 사용자 메모를 추가했다. 멱등 키와 산출물 ID를 함께 기록하고 이전 분석 메모도 보존한다. 메모는 승인이나 검토 완료가 아니며 `REVIEW_REQUIRED`, 정책 및 운영 설정을 바꾸지 않는다. `GET /api/policy-work/{work_id}`는 후속 Run/Evidence와 메모를 반환한다.
- 전용 PostgreSQL·Redis·OpenSearch 전체 `python -m pytest -q tests`: **33 passed, warning 1건**. 이후 워커 시작 시 구형 작업 복구와 자료형 검증을 더한 뒤 `python -m pytest -q tests/test_policy.py`: **5 passed, warning 1건**. `npm run build`, `docker compose config --quiet` 통과. 별도 빈 Compose에서 `python -u tests/e2e_stack.py`는 세 시나리오, 첫 후속 검사, 메모의 중복 방지, 적용·롤백 및 회귀 차단을 통과했다. E2E는 워커 시작 복구 추가 전 실행했다.
- 별도 프로젝트 브라우저에서 검사 `SUCCEEDED`, 후속 DAG의 대기 상태, 메모 입력·저장과 `REVIEW_REQUIRED` 유지 안내를 확인했다. `docker compose -p workos-p0 up -d --build`로 기존 데이터를 유지하면서 갱신한 후 기존 이벤트의 첫 후속 Work가 `COMMITTED` / Run `SUCCEEDED`임을 API로 확인했다.

제약: 검색 인덱스 갱신과 회귀 테스트 Work는 아직 BLOCKED이며 담당자 검토 Work도 WAITING_APPROVAL이다. 메모 기록은 이 작업들의 완료를 가장하지 않는다. 정책·VOC 데이터와 평가 사례는 합성이고, 검사 통과가 정책 해석의 의미적 정확성을 보장하지 않는다. 기존 Graph 체크포인트의 워커 종료 후 자동 재개 제약도 남아 있다.

다음 세 단계: ① 가상 신규 정책 버전의 검색 인덱스 갱신과 독립 검증을 후속 Work로 구현한다. ② 고정 VOC 평가셋의 영향 재검사와 명시적인 사용자 검토 완료를 연결한다. ③ 워커 소유권과 lease fencing을 보강해 안전한 체크포인트 복구를 구현한다.

## 2026-10-31 추가 진행 — LangGraph 단계·체크포인트와 실제 모델 재검증

현재 세 가지 가상 시연 흐름은 실제 API·워커·PostgreSQL·OpenSearch를 통해 재현할 수 있다. 전체 `init.md` 제품 계약과 운영 복구는 아직 미완료다. 이 절이 이전 기록보다 최신이다.

- `backend/app/runtime/graph.py`, `011_graph_runtime.sql`: TASK-001/002/003의 실행별 `inspect_context → draft → finalize` LangGraph와 PostgreSQL 체크포인트, 추가 전용 `run_steps`를 구현했다. Run 컨텍스트 해시 및 살아 있는 lease를 확인하고 단계 예외에는 예외 유형만 남긴다. 후보의 최종 채택과 공식 Work 상태·이벤트는 기존 커널과 독립 검증기가 담당한다. LangSmith 추적은 비활성화했다. 기존 컨텍스트의 업무별 `graph_version`은 그대로 보존하고 실행 단계에 `workos-langgraph-v1`을 별도 기록한다.
- `GET /api/runs/{id}`와 대시보드 이력에 최근 Run의 단계 기록을 추가했다. 과거 Run에는 기록이 없다고 표시한다. 검증기 PASS와 모델 해석의 미검증 상태를 구분한다.
- `backend/app/integrations/search.py`: 인덱스 생성만으로 `connected`를 반환하던 초기화 경합을 수정했다. 필수 가상 문서 3개가 검색되기 전에는 `indexing`이다. 최초 빈 환경 E2E에서 이 경합으로 첫 브리핑이 검증 실패했고, 수정 후 빈 환경을 다시 만들어 재실행했다.
- 실제 수행: 전용 PostgreSQL·Redis와 OpenSearch를 사용한 `python -m pytest -q tests` **31 passed, warning 1건**. 이후 준비 상태 변경을 더한 `python -m pytest -q tests/test_search_readiness.py tests/test_graph_runtime.py` **3 passed**. 그래프 테스트는 초안 작성 후 의도적으로 실패시키고 별도 Python 프로세스에서 동일 Run을 재개해 앞 단계가 반복되지 않고 커널 검증을 통과함을 확인했다. 전체 테스트는 준비 상태 수정 이전에 실행했다.
- `npm run build`, `docker compose config --quiet` 성공. 별도 빈 Compose 프로젝트의 `python -u tests/e2e_stack.py`는 세 흐름 및 각 그래프 단계, 출처 실패 후 새 Run 재시도, 명시적 적용·롤백과 회귀 차단을 통과했다. `python -u tests/live_llm_smoke.py`는 실제 `gpt-6-luna`, `DEMO DATA`, `LIVE_LLM`, `market-validator-v3 PASS`, `workos-langgraph-v1`을 확인했다. 브라우저 이력에서도 단계와 실제 모델 표시를 확인했다. `.env.apikey` 내용은 출력하지 않았다.
- 기존 데이터를 유지하며 `docker compose -p workos-p0 up -d --build`로 실행 중인 환경을 갱신했다. 기존 환경에서 새 MOCK 시장 작업이 `COMMITTED` / Run `SUCCEEDED`이고 그래프 기록 6건인 것을 API로 확인했다. 검증용 별도 프로젝트와 전용 테스트 컨테이너만 제거했다.

제약: 체크포인트 재개는 **유효한 동일 Run에 대한 내부 실행 경로와 별도 프로세스 테스트**로 확인했다. 워커가 죽거나 lease가 만료되면 현재 커널은 기존 Run을 FAILED로 보존하고 새 Run을 만든다. 자동으로 옛 Run을 이어 붙이지 않는다. 외부 모델 응답 후 체크포인트 저장 전에 실패하면 모델 호출이 재실행될 수 있으므로 정확히 한 번 호출을 보장하지 않는다. TASK-002 후속 Work는 여전히 대기 중이며 VOC 평가 사례와 시장 데이터는 합성이다. 마이그레이션 down SQL은 실행 검증하지 않았고 체크포인트 이력은 의도적으로 보존한다.

다음 세 단계: ① 워커 소유권·lease fencing과 실패 분류를 설계해 안전한 자동 체크포인트 재개를 구현한다. ② TASK-002 후속 DAG의 실행과 사용자 규정 검토를 연결한다. ③ TASK-003 추천의 중복·품질 추세와 브라우저 자동 회귀를 보강한다.

## 2026-10-31 추가 진행 — 검색 재검증·별도 회귀셋·신규 환경 E2E

현재 **세 가지 가상 시연 흐름은 실행 가능**하며, 전체 `init.md` 제품 계약은 미완료다. 이 절이 이전 기록보다 최신이다.

- `backend/app/integrations/search.py`, `backend/app/work/policy.py`는 가상 정책의 이전 버전 v1을 OpenSearch에서 조회하고 원본 저장소의 v2와 비교한다. v2 인덱스 갱신은 후속 Work로 남아 있으며 실제로 수행하지 않았다. `policy-validator-v2`는 검색 결과·조항·출처·DAG를 검증한다. 동일 Work에 재검증을 요청하면 과거 승인 산출물을 보존한 채 새 Run을 만든다.
- `voc-classifier-eval-v2`는 동일 기본 평가 24건과 별도 회귀 사례 6건을 모두 검사한다. 사전 정의된 `challenger-overfit`은 기본 24/24여도 별도 회귀 2건이 발생해 FAIL이며 적용이 막힌다. 두 평가셋과 방법 해시를 기록하고 평가 기준이 바뀌면 이전 평가의 적용을 막는다. 별도 사례도 소스에 포함된 합성 데모로, 실제 미공개 고객 데이터가 아니다.
- `tests/conftest.py`는 동시에 실행되는 테스트 프로세스가 같은 임시 PostgreSQL DB를 비우지 않도록 세션 advisory lock을 잡는다. 초기에는 두 테스트 프로세스를 같은 DB에서 겹쳐 실행해 상호 간섭 실패가 발생했으므로 해당 결과를 폐기했고, 수정 후 단독 전체 실행과 두 프로세스 동시 실행을 별도로 검증했다.
- 별도 프로젝트 `workos-verify-01a19216`을 다른 포트·빈 PostgreSQL/Redis/OpenSearch로 기동하고 `tests/e2e_stack.py`를 실제 HTTP API와 워커에 실행했다. TASK-001 차이 30/출처 누락·복구, TASK-002 중복 이벤트/네 대기 DAG, TASK-003 18/24 대 24/24/적용·롤백 및 별도 회귀 실패·기각이 모두 통과했다. 별도 포트의 브라우저가 해당 API에 연결되는 것도 확인했다. 검증용 프로젝트·볼륨만 제거하고 기존 `workos-p0` 데이터와 서비스는 보존했다.
- 검증: 전용 PostgreSQL·Redis 및 임시 OpenSearch 인덱스 전체 테스트 **29 passed**, Starlette/anyio deprecation warning 1건. `npm run build`, `docker compose config --quiet`, Compose 기동 및 실제 대시보드 확인도 성공했다. 브라우저에서 정책 재검증의 새 Run `SUCCEEDED`, `OPENSEARCH`, `policy-validator-v2`를 확인했다.

다음 세 단계: ① LangGraph 실행 단계 및 중단/재개를 실제 Work 상태와 분리해 구현한다. ② TASK-002 후속 Work 실행과 사용자의 규정 검토를 실제 처리하되 운영 정책을 자동 변경하지 않는다. ③ TASK-003의 이전 추천 중복 여부·품질 추세, 실제 출처/평가 UI와 자동 브라우저 검증을 보강한다. 현재 정책 후속 Work는 대기 중이고 VOC 평가·후보는 합성 데모이며 LangGraph 및 실제 사내 데이터 연계는 없다.

## 2026-10-31 추가 진행 — 출처 변경 시 개선안 무효화와 시연 가이드

현재 세 가지 흐름의 가상 데모가 실행되며, **전체 제품 계약은 아직 미완료**다. 이 절이 이전 기록보다 최신이다.

- `010_improvement_source_snapshot.sql`과 `backend/app/evaluation/improvements.py`에서 개선안에 참조 규정의 버전·검토 상태를 고정했다. 보고서 생성 후 참조 규정이나 현행 방법이 바뀌면 해당 보고서의 새 피드백과 기존 개선안의 평가/적용을 거부한다. 새 방법의 적용·롤백 뒤 ID가 우연히 같아져도 상태 버전이 다르면 이전 평가를 적용하지 않는다. 010 이전에 만든 개선안은 이 스냅샷이 없어 새 적용을 허용하지 않으며 과거 이력은 유지한다.
- 최종 Compose 화면에서 `REVIEW_REQUIRED` 조건으로 생성된 현행 VOC 보고서의 피드백, 동일 평가와 PASS 후보가 `PROPOSED`에 남는 것을 확인했다. 자동 적용은 하지 않았으며 현재 후보는 사용자가 직접 선택할 수 있다.
- `docs/DEMO.md`에 세 흐름의 실제 클릭 순서, 예상 결과, 비파괴적인 별도 Compose 프로젝트 시연 방법과 MOCK/LIVE 구분을 기록했다. 기존 프로젝트 데이터는 보존한다.
- PostgreSQL·Redis·임시 OpenSearch 인덱스를 사용한 전체 테스트: **26 passed**, Starlette/anyio deprecation warning 1건. `npm run build`, `docker compose -p workos-p0 up --build -d`: 성공. 010 순방향을 테스트 DB와 Compose DB에 적용했으며 down SQL은 실행 검증하지 않았다.

다음 세 단계: ① 자동 브라우저 E2E와 처음부터 재현하는 별도 환경을 검증한다. ② TASK-002 정책 문서의 검색 연계와 대기 중인 후속 Work/담당자 검토를 실제 처리한다. ③ LangGraph 실행 단계·재개와 별도 미공개 회귀 사례를 추가한다. 현재 검색은 가상 시장 문서의 ID 검색이고, 규정 후속 Work는 미실행이며, VOC 점수는 공개된 고정 합성 사례의 데모 점수다.

## 2026-10-31 추가 진행 — 실제 검색 및 원천 읽기 권한

현재 단계: **세 가상 데모의 핵심 흐름 시연 가능, P1 전체 수락 조건은 검증 중**. 이 절이 이전 기록보다 최신이다.

### 구현과 실제 확인

- `backend/app/integrations/search.py`, Compose `search-seed`로 PostgreSQL에 보존된 가상 시장 문서 두 개를 OpenSearch `workos-demo-documents-v1`에 시드한다. TASK-001은 실제 OpenSearch 문서 ID 검색 결과와 질의·인덱스·문서 해시를 Run 컨텍스트에 기록한다. 검색 불가 시 DB 결과로 조용히 대체하지 않는다.
- `market-validator-v3` / `market-suite-v3`는 검색 스냅샷과 원본 문서 버전·해시의 일치 및 제출 시점의 원본/검색 상태를 다시 확인한다. 누락되거나 변경된 출처는 발행하지 않는다. 과거 검증기 v1/v2의 결과는 이전 Evidence에 그대로 남는다.
- `009_demo_source_reader.sql`, `backend/app/integrations/sql_source.py`로 TASK-001 시장/문서와 TASK-003 VOC 조회에 로컬 PostgreSQL 읽기 전용 역할을 사용한다. 시드 및 검증·상태 쓰기는 별도 내부 DB 연결을 사용한다. 고정된 로컬 개발용 암호이며 운영 보안 설정은 아니다.
- 실제 Compose에서 검색 시드가 가상 문서 2개를 인덱싱하고 `/api/health`가 `OPENSEARCH / connected`를 반환했다. 실제 LIVE LLM TASK-001 Run은 `gpt-6-luna`, `DEMO DATA`, `OPENSEARCH`, `POSTGRES_READ_ONLY_ROLE`, `market-validator-v3 PASS`를 기록했다. TASK-003 Run도 읽기 전용 역할로 조회하고 검증을 통과했다. 브라우저 보고서에 LIVE LLM과 OpenSearch, 검사 결과가 표시되는 것을 확인했다.

### 실제 검증

- 전용 PostgreSQL·Redis 및 별도 임시 OpenSearch 인덱스에서 `python -m pytest -q tests`: **25 passed**, Starlette/anyio deprecation warning 1건. OpenSearch에서 문서 누락→실패→복구·재시도, 검색 후 문서 삭제 시 발행 거부, PostgreSQL 원본 변경 감지, 읽기 전용 역할의 UPDATE 거부를 포함한다.
- `npm run build`, `docker compose config --quiet`, `docker compose -p workos-p0 up --build -d`: 성공. 009 순방향 마이그레이션은 테스트 DB와 Compose DB에 적용했다. down SQL은 실행 검증하지 않았다.

### 제약과 다음 세 단계

1. 세 흐름을 새 데이터 환경에서 재현하는 자동 브라우저 E2E와 비파괴 시연 초기화 방법을 추가하고, 화면의 각 결과가 실제 Evidence/Run과 연결되는지 검증한다.
2. TASK-002 정책 문서의 검색 연계와 후속 Work 처리·담당자 검토를 연결하고, 규정 검토 대기 상태가 후속 보고서의 신뢰 수준에 미치는 영향을 명확하게 한다.
3. LangGraph의 Run 단계 기록·실패 재개와 TASK-003의 별도 미공개 회귀 사례, 평가·버전 변경에 대한 추가 동시성 검증을 보강한다.

검색은 가상 문서의 정확한 ID 조회이며 실제 뉴스 검색 또는 실데이터 연계가 아니다. 정책 변경의 검색 인덱스 갱신 후속 Work는 대기 중이다. TASK-003 점수와 후보는 고정된 합성 데모이며 사람의 유용성 평가가 아니다. SQL 역할은 로컬 데모 권한 분리이고 운영용 비밀·IAM 체계가 아니다.

## 2026-10-31 추가 진행 — TASK-003 피드백·평가·적용/기각

현재 단계: **세 가지 가상 데모의 핵심 상호작용 구현 및 검증 중**. P1 전체 완료는 아니다. 이 절이 이전 기록보다 최신이다.

### 구현과 실제 관찰

- `007_voc_improvement.sql`, `008_improvement_state_version.sql`, `backend/app/work/voc.py`와 `backend/app/evaluation/improvements.py`에 가상 VOC 24건, 고정 분류 평가 24건, 해시가 고정된 방법 버전, 피드백·개선안·평가·명시적 적용/기각/롤백 이력을 추가했다. TASK-003 v2는 보고서와 추천별 근거 ID, 미분류 목록, 원천 스냅샷, 규정 검토 상태, 결정론적 Evidence를 남긴다.
- 사용자가 기존 보고서의 추천에 피드백을 남기면 미리 준비된 단일 분류 규칙 후보의 개선안을 만든다. 같은 평가 사례와 정답을 두 방법에 실행하고 각 예측, 정답, 퇴행·개선 ID, 평가셋·방법 해시, 평가기 버전을 보존한다. 통과 조건은 정확도 개선과 퇴행 0건이다. 실제 서비스·인간 평가 점수는 아니다.
- Compose 대시보드에서 Champion v1의 18/24(75.0%), 개선 후보 24/24(100.0%), 퇴행 0건을 확인했다. `적용` 후 다음 보고서가 `challenger-v2`를 사용하고 미분류 6건이 0건으로 바뀌었다. `롤백` 후 다음 Run은 `champion-v1`을 사용했다. 최종 검증 후 활성 방법은 원래 Champion으로 복원했다.
- 별도 퇴행 후보는 17/24(70.8%), 퇴행 3건으로 FAIL이며 UI의 적용 버튼이 비활성화되고 기각을 선택할 수 있음을 확인했다. 과거 방법으로 만든 보고서에는 피드백을 남길 수 없으며, 적용/롤백으로 상태 버전이 바뀌면 이전 평가를 재적용할 수 없다. 데모 초기 검증 중 위 제한 도입 전 생성한 이력도 삭제하지 않고 보존했다.
- TASK-002의 문서 변경이 TASK-003 실행 도중 도착하면 진행 중 Run을 실패로 보존하고 `REWORK_REQUIRED`로 전환하며 늦은 결과의 발행을 막는다.

### 검증

- 전용 PostgreSQL 16·Redis 7에서 `python -m pytest -q tests`: **22 passed**, Starlette/anyio deprecation warning 1건. 동일 평가셋, 통과/퇴행, 명시적 적용, 다음 Run, 기각, 롤백, 평가셋 변경, Champion 변경 후 오래된 평가 거부, 정책 변경 중 실행 중단을 확인했다.
- `npm run build`, `docker compose config --quiet`, `docker compose -p workos-p0 up --build -d`: 성공. TASK-002와 TASK-003 전체 경로는 실제 Compose/브라우저에서도 클릭하고 결과를 확인했다. 007/008 순방향 마이그레이션을 적용했으며 down SQL은 실행 검증하지 않았다.

### 제약과 다음 세 단계

1. TASK-001/002의 OpenSearch 문서 시딩·검색과 PostgreSQL 원천 읽기 전용 권한을 연결하고, 출처 삭제·버전 불일치 재검증을 실제 연계로 시험한다.
2. 세 데모를 처음부터 재현할 수 있는 사용자용 초기화·시연 절차와 브라우저 기반 E2E를 정리하고, 각 실행의 연결 상태·근거 탐색을 개선한다.
3. 정책 변경 후속 Work를 실제 로컬 작업/사용자 검토 흐름에 연결하고, LangGraph 실행 기록·재개 및 실제 데이터 어댑터는 별도 범위에서 확장한다.

현재 데이터와 평가셋은 합성이며 후보 두 개는 사전 정의된 규칙이다. TASK-003의 `REVIEW_REQUIRED`는 보고서에 표시되지만 보고서의 결정론적 검사 자체를 막지는 않는다. TASK-002 후속 단계는 아직 실행되지 않았고, OpenSearch 검색/시딩·LangGraph·실데이터 커넥터는 미구현이다. LIVE LLM은 TASK-001의 해석에 한정되며 의미적 진실성은 별도 검증하지 않는다.

## 2026-10-31 추가 진행 — TASK-002 문서 변경과 Work DAG

현재 단계: **P1의 TASK-001/002 시연 경로 구현 중**. TASK-003과 OpenSearch 검색 연결 등은 아직 남아 있다. 이 절이 이전 기록보다 최신이다.

### 완료 및 확인

- `backend/migrations/006_policy_workflow.sql`에 불변 가상 정책 문서 버전, Task-문서 의존성, 변경 이벤트, 후속 Work 관계를 추가했다. `backend/app/work/seed.py`는 가상 `POLICY-VOC` v1/v2와 TASK-003의 v1 의존성을 시드하고 TASK-002의 새로운 계약 버전을 등록한다. 기존 TASK-002 v1과 과거 이력은 보존한다.
- `POST /api/events/document.updated`는 문서 버전과 SHA-256을 확인하고, Task를 `REVIEW_REQUIRED`로 표시하며, 분석 Work와 네 개의 연결된 후속 Work를 하나의 트랜잭션에서 생성한다. 같은 이벤트의 재전달과 다른 이벤트 ID를 가진 동일 문서 변경은 기존 Work를 반환한다. 영향받은 기존 `COMMITTED` Work와 그 자손은 stale 처리한다.
- `backend/app/work/policy.py`와 `backend/app/work/service.py`는 문서·조항의 원본/버전/해시, 변경 내역, 출처 위치, DAG를 스냅샷으로 보존한다. 결정론적 조항 비교, 출처, DAG 및 해시 검증을 통과한 분석만 `COMMITTED`되고 Evidence/Evaluation이 남는다. 후속 단계는 `BLOCKED` 세 개와 `WAITING_APPROVAL` 한 개로 표시하며, 아직 실행하거나 검색 인덱스를 갱신하지 않는다.
- Compose 대시보드에서 실제 `POLICY-VOC` v1 조회 후 변경 이벤트를 발생시켰다. `TASK-003` 상태가 `CURRENT → REVIEW_REQUIRED`, 분석 Work/Run이 `COMMITTED`/`SUCCEEDED`로 변경되었고, 보관기간 `90일 → 30일`, 검토주기 `매월 → 매주`, 출처 위치와 네 단계 DAG가 표시됐다. 이벤트를 다시 눌렀을 때 기존 작업을 반환하는 UI 메시지를 확인했다. 이는 가상 내부 규정이며 법규 해석이 아니다.

### 실제 검증

- 전용 PostgreSQL 16과 Redis 7에서 `python -m pytest -q tests`: **18 passed**, Starlette/anyio deprecation warning 1건. 신규 테스트는 이벤트 동시 중복 전달, 잘못된 해시 거부, 기존 승인 Work의 stale 전환, 출처·DAG 검증, 변조된 변경 내역의 발행 거부를 확인했다. 최초 좁은 테스트에서는 시드의 이름 충돌로 2건 실패했고 수정 후 통과했다.
- `npm run build`, `docker compose config --quiet`, `docker compose -p workos-p0 up --build -d`: 성공. 브라우저에서 변경 전 상태, 이벤트 발생, 분석 완료, 후속 작업 및 중복 방지 동작을 확인했다. 006 순방향 마이그레이션은 테스트 DB와 Compose DB에서 적용됐고 down SQL은 아직 실행 검증하지 않았다.

### 현재 제약과 다음 세 단계

1. TASK-003의 익명 VOC 시드와 근거가 연결된 추천 보고서를 구현하고, 사용자 피드백을 Work 및 Evidence에 연결한다.
2. 고정 평가셋 20건 이상에서 Champion과 단일 변경 Challenger를 동일 입력으로 비교해 퇴행과 통과 조건을 표시하고, 명시적인 적용·기각 및 롤백을 구현한다.
3. 세 흐름의 UI 기반 E2E와 OpenSearch 문서 시딩·검색, 원천 데이터 읽기 전용 권한 및 운영 가이드를 보강한다.

OpenSearch는 아직 연결되지 않았고, 정책 후속 Work 실행·담당자 검토 처리와 TASK-003은 아직 없다. 이벤트를 받은 것만으로 Task/Prompt/운영 정책을 자동 변경하지 않는다. 두 개의 정책 버전만 시드되어 있으며 이미 처리된 같은 변경은 반복 시 기존 Work를 보여준다.

## 2026-10-31 추가 진행 — TASK-001 및 LIVE LLM

현재 단계는 **P0 잔여 작업과 P1 TASK-001 수직 경로 진행 중**이다. 아래의 이전 상태 기록보다 이 절이 최신이다. P0 전체 또는 세 데모 완료는 아니다.

### 구현 및 확인

- `.env.apikey`의 `openai_api_key`/`openai_model`을 워커에만 주입하고 `.gitignore`로 보호했다. 별도 최소 실제 OpenAI Responses 호출에서 `gpt-6-luna`, `completed`, 입력 10/출력 5 토큰, 응답 존재를 확인했다. 키는 출력하거나 저장하지 않았다. 모델 출력 구조화 방식은 [OpenAI 공식 Structured Outputs 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 참고했다.
- TASK-001 v2 계약을 추가하고 기존 v1과 과거 Work/Run을 보존했다. `004_market_workload.sql`, `005_artifact_version.sql`로 가상 시장 관측값, 가상 문서, Run 컨텍스트, Evaluation, 보고서 버전을 추가했다. 마이그레이션 순방향은 별도 PostgreSQL 테스트 DB와 Compose에서 적용했으며 down SQL은 아직 검증하지 않았다.
- `backend/app/work/market.py`는 버전별 SQL 조회, 원시 값/조회 조건/해시/문서 ID·버전·본문·해시와 모델/프롬프트/그래프 버전을 Run 컨텍스트로 기록한다. 모델에는 가상 데이터만 전달한다. 실제 모델의 해석은 `LIVE_LLM`, 가상 자료는 `DEMO DATA`, 의미적 판단은 `NOT_VERIFIED`로 분리한다. `backend/app/work/service.py`의 독립 수치 재계산, 스키마·해시·필수 출처 검사 및 Evaluation/Evidence를 통과해야 커밋된다. 모델 문장 내용의 진실성이나 인과관계는 검증하지 않는다.
- `backend/app/worker.py`, `backend/app/api/main.py`, `frontend/src/App.vue`는 데이터 버전과 MOCK/LIVE LLM 선택, 보고서/근거 조회, 통과된 두 Run의 비교, 첫 시도 출처 누락 및 같은 Work 재시도를 연결한다. 출처 스냅샷과 쿼리 해시는 Run API에서 확인할 수 있다.
- 실제 Compose 워커로 v1 MOCK과 v2 LIVE LLM을 각각 실행해 둘 다 COMMITTED/SUCCEEDED, LIVE 모델 `gpt-6-luna`, 산출물 `DEMO DATA`/`LIVE_LLM`, 결정론적 검사 PASS, 의미 검증 `NOT_VERIFIED`를 확인했다. USD/KRW 가상 값은 1350 → 1380, 차이 30이다. 출처 누락은 `VERIFICATION_FAILED`/`REWORK_REQUIRED`였고, 같은 Work 재시도에서 새 Run이 성공했으며 실패 시도가 남았다. 브라우저에서 v1→v2 비교의 변화 30도 확인했다. 이후 독립 수치 재계산을 추가하고 검증기/평가셋을 각각 `market-validator-v2`/`market-suite-v2`로 올린 다음 LIVE LLM Run의 모든 결정론적 검사 통과를 다시 확인했다.

### 실행한 검증

- 전용 PostgreSQL 16과 Redis 7에서 `python -m pytest -q tests`: **16 passed**, Starlette/anyio deprecation warning 1건. 누락 출처, 수치 변조, 비교, 재시도, 키 미설정 시 자동 MOCK 전환 금지와 기존 커널 테스트를 포함한다.
- `npm run build`: 성공. `docker compose -p workos-p0 up --build -d`: 성공. API를 통해 MOCK/LIVE 요청, 비교, 실패 및 복구를 실제 워커에서 확인했다. 화면 비교/근거/모드 구분을 브라우저에서 확인했다.

### 현재 제한과 다음 세 단계

- 자료는 모두 합성 데모이며 LIVE는 모델 호출만 의미한다. OpenSearch 검색, 문서 이벤트, 정책·VOC 데이터와 TASK-002/003, LangGraph, 실데이터 커넥터, LLM 의미적 판단 검증 및 운영 수준 보안은 미구현이다. TASK-001의 SQL 접속 권한은 DB 역할 차원의 읽기 전용으로 아직 분리하지 않았다. LIVE 실패는 비밀 노출 방지를 위해 예외 유형만 기록한다.
1. TASK-001의 SQL 읽기 전용 역할과 OpenSearch 문서 시딩·검색, 문서 최신성/삭제 검증 및 UI 출처 탐색을 추가하고 실패·복구 E2E를 보강한다.
2. TASK-002의 멱등 `document.updated` 이벤트, 버전 비교, 영향 DAG 생성 및 검토 필요 UI를 구현한다.
3. TASK-003의 VOC/고정 평가셋과 피드백, 동일 조건 Champion/Challenger 비교, 명시적 적용·기각·롤백을 구현한다.

## 이전 P0 마일스톤 기록

기준: 2026-10-31. 단계: **P0 커널 기반 구현 중**. P0 전체 및 P1 세 데모는 아직 완료되지 않았다.

## 완료 및 실제 확인한 수락 조건

- `TaskDefinition` 버전, `JobDefinition`, 영속 `WorkItem`, 시도별 `Run`, `Artifact`, `Evidence`, 의존성, 이벤트와 큐를 PostgreSQL에 저장한다. 001~003 마이그레이션은 빈 PostgreSQL과 Compose DB에서 실제 적용했다. 각 down SQL은 작성했으나 실행 검증하지 않았다.
- 상태 전이와 이벤트 기록을 한 트랜잭션에서 처리한다. 이벤트는 UPDATE/DELETE를 DB 트리거로 거부한다. 동일 idempotency key와 동일 내용은 동일 Work를 반환하고 다른 내용은 거부한다. 동시 트리거와 동시 claim은 PostgreSQL 잠금으로 직렬화한다.
- 실패 및 검증 실패를 남기고 재시도는 같은 Work의 새 Run을 만든다. 리스가 만료된 Run은 실패로 보존하고 재청구하며 늦은 워커의 결과는 거부한다. 테스트 전용 영수증 검증기에서 실패한 결과는 발행하지 않고 이전 승인 Artifact를 보존한다. 실제 세 Task는 전용 검증기가 없으므로 어떤 영수증으로도 COMMITTED되지 않는다.
- DAG 순환을 막고 선행 작업의 승인 Artifact 버전을 고정한다. 선행 Work 변경 시 영향받은 후손만 stale 또는 blocked/rework 처리하고, 실행 중인 후손의 Run은 실패로 보존한다. 사용자 재검토 요청 시 최신 승인 버전으로 고정한다.
- PostgreSQL 큐와 Redis Streams 알림, 별도 워커, DB에 cursor를 보존하는 Cron 스케줄러를 연결했다. 동일 예약 시각이 중복 발화해도 Work는 하나다. 알림의 중복이나 Redis 부재는 PostgreSQL 폴링으로 복구한다. 큐는 외부 부작용의 정확히 한 번 실행을 보장하지 않는다.
- TASK-001~003의 정의와 Job 메타데이터를 시드하고 MOCK 표시가 있는 Vue 대시보드, 수동 실행과 이력 API를 연결했다. 브라우저에서 TASK-002 실행 후 `READY → REWORK_REQUIRED`, Run `FAILED` 및 `MOCK: task workload and acceptance validator are not implemented`가 상세 이력에 자동 표시되는 것을 확인했다.
- Compose의 API, UI, PostgreSQL, Redis, OpenSearch, 워커, 스케줄러가 기동했다. API health는 MOCK/database connected를 반환했고 Redis는 PONG, OpenSearch는 2.19.3 응답을 반환했다. 검색 연계까지 확인했다는 의미는 아니다.

## 주요 경로와 결정

- `backend/migrations/001_kernel.sql`, `002_scheduler.sql`, `003_queue_outbox.sql`: 스키마와 마이그레이션.
- `backend/app/domain/contracts.py`: 상태와 Pydantic 계약. `backend/app/work/service.py`: 전이·이벤트·중복·DAG·리스·발행 게이트.
- `backend/app/work/notifications.py`, `backend/app/worker.py`, `backend/app/scheduler.py`: Redis Streams 알림, 내구성 있는 PostgreSQL 큐, 워커와 Cron cursor.
- `backend/app/integrations/artifact_store.py`: 본문과 SHA-256을 PostgreSQL 트랜잭션에 저장하는 개발용 어댑터. MinIO 이미지 pull이 거부되어 별도 MinIO 대신 이 어댑터를 사용한다.
- `backend/app/work/seed.py`, `backend/app/api/main.py`, `frontend/src/App.vue`: 세 Task 메타데이터, API, 대시보드. `docker-compose.yml`, `.env.example`, `README.md`: 로컬 실행.
- `tests/test_kernel.py`, `test_scheduler.py`, `test_redis.py`, `test_api.py`: 실제 PostgreSQL/Redis를 이용한 핵심 경로 검증.

## 실제 실행한 검증

- 전용 PostgreSQL 16과 Redis 7 컨테이너를 각각 `localhost:55432`, `localhost:56379`에 띄운 뒤 `WORKOS_TEST_DATABASE_URL`과 `WORKOS_TEST_REDIS_URL`을 지정하고 `python -m pytest -q tests`: **13 passed**, Starlette/anyio deprecation warning 1건. 최초 실행에는 Work 상세 이벤트 조회의 UUID/text 비교 오류가 1건 있었으며 수정 후 재실행했다.
- `npm install`, `npm run build`: 성공, Vue/Vite 프로덕션 빌드 산출. `docker compose config --quiet`: 성공.
- `docker compose -p workos-p0 up --build -d`: 전체 서비스 시작 성공. `docker compose -p workos-p0 ps`, `Invoke-RestMethod http://localhost:8000/api/health`, `docker compose -p workos-p0 exec -T redis redis-cli ping`, `Invoke-RestMethod http://localhost:9200`: 서비스 상태 및 응답 확인. 컨테이너 기동 직후 OpenSearch는 준비 중이었으나 이후 HTTP 응답을 받았다.
- `docker compose -p workos-p0 exec -T postgres psql -U workos -d workos -c 'SELECT version FROM schema_migrations ORDER BY version'`: 001, 002, 003 적용 확인. 전용 테스트 컨테이너 두 개는 검증 후 제거했으며 Compose 서비스는 실행 중이다.
- 화면에서 실행 요청과 실제 DB 기반 실패 이력 및 자동 갱신을 확인했다. 사용자 관찰 테스트는 실제 데모 결과 생성이나 평가 통과가 아니다.

## 제약과 불변식

- P0 미완료: 도메인 샘플 데이터/문서 시드, OpenSearch 검색 어댑터, 이벤트 Webhook, 실제 Task 검증 정책 및 LIVE 어댑터가 없다. `WORKOS_MODE=LIVE`는 시작 시 명시적으로 거부한다. 세 Task의 입력/출력 스키마는 아직 빈 객체다. 결과 비교, 평가, 개선 제안 등은 P1 작업이다.
- 기본 워커는 업무 처리기가 없음을 실패로 기록할 뿐 보고서를 만들지 않는다. 테스트 전용 검증기는 실제 금융/VOC 검증이 아니다. Dashboard 연결 상태는 설정 설명이며 Redis/OpenSearch의 실시간 상태 진단은 아니다.
- Cron은 `Asia/Seoul` 평일 표현이며 공휴일 캘린더가 없다. 첫 스케줄러 기동 전 시각은 소급하지 않는다. 운영 중지 후 저장된 커서는 재시작 시 처리한다.
- 작업별 세밀한 동시성 한도, 이벤트 Webhook의 멱등성, 외부 데이터 버전 변경 감지와 장시간 작업의 lease 갱신, Redis Streams pending 정리/보존 정책은 아직 없다. 전역 advisory lock은 초기 단일 사용자용이며 확장성 검증을 하지 않았다.
- API 응답의 `request_id` 표준화, 모든 식별자 입력 유효성, UI의 DAG/재시도 조작과 실제 평가 상태는 후속 작업이다. 비밀번호 및 보안 설정은 로컬 개발 전용이다.

## 다음 세 단계

1. P0 잔여: Task별 입력/출력 계약, 결정론적 금융·문서·VOC 시드, OpenSearch 문서 시딩 및 연결 상태 진단을 추가한다.
2. TASK-001 수직 경로: 시장 데이터 조회 → 계산 → 버전 산출물 → 출처/수치 검증 → 실패·재시도·전회 비교를 API와 UI까지 구현하고 E2E로 확인한다.
3. TASK-002의 멱등 document.updated Webhook과 변경 영향 DAG를 연결한 뒤 TASK-003 고정 평가셋 및 명시적 적용/기각 경로를 구현한다.
