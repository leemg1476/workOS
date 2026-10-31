# Work OS v0.1 — 작업 커널과 TASK-001 데모

로컬 단일 사용자용 업무 커널입니다. TASK-001은 가상 시장 데이터와 문서를 사용해 보고서를 생성하고 수치·출처를 검증합니다. 해석 문장은 결정론적 MOCK 또는 실제 OpenAI 모델로 생성할 수 있습니다. 실제 모델을 선택해도 원천 데이터는 **DEMO DATA**입니다. TASK-002는 가상 정책 변경을 분석하고 후속 Work DAG를 생성합니다. TASK-003은 가상 VOC 분류·피드백·고정 평가셋 비교와 사용자 적용/기각/롤백을 보여줍니다.

## 시작

Docker Desktop을 실행한 후 저장소 루트에서:

```powershell
docker compose -p workos-p0 up --build -d
```

- 대시보드: http://localhost:5173
- API 문서: http://localhost:8000/docs
- 상태: http://localhost:8000/api/health
- 재현 순서와 별도 데이터 환경: [3분 데모 가이드](docs/DEMO.md)
- 중지: `docker compose -p workos-p0 down` (PostgreSQL과 Redis 볼륨은 유지)

초기 마이그레이션과 세 Task 정의는 시작 시 자동 적용됩니다. 원천 데이터 모드는 `WORKOS_MODE=MOCK`만 지원하며 `LIVE` 데이터 모드는 시작 시 거부합니다. `.env.example`을 참고하세요. 이 구성은 로컬 개발용입니다.

TASK-001 화면에서 데이터 `v1` 또는 `v2`와 해석 생성 방식 `MOCK` 또는 `LIVE LLM`을 선택하고 실행합니다. 출처 누락 테스트를 선택하면 첫 시도는 검증 실패하며, 이력에서 같은 Work를 재시도하면 전체 출처로 다시 검사합니다. 검증에 통과한 보고서는 비교 메뉴에서 선택할 수 있습니다. 모델 해석은 별도의 의미 검증을 받지 않습니다. Cron은 서울 시간의 평일만 구분하고 한국 공휴일은 처리하지 않습니다.

TASK-002의 `규정과 영향 보기`에서 가상 문서 v1/v2와 연결된 업무를 확인하고 `변경 이벤트 발생`을 누르면 변경 조항과 출처, 영향받은 Task, 네 개의 후속 Work DAG가 나타납니다. 같은 변경을 다시 발생시켜도 기존 Work를 반환합니다. 후속 작업은 대기·검토 필요 상태이며 검색 인덱스나 운영 설정이 실제로 바뀌지는 않습니다.

TASK-003을 실행하면 가상 VOC 추천과 근거 ID, 미분류 건수가 표시됩니다. 피드백 사유와 미리 준비된 후보를 선택해 추천에 피드백을 남긴 다음 `동일 평가 실행`을 누릅니다. 통과 후보는 사용자가 `적용`해야 다음 Run에 반영되며 롤백할 수 있습니다. 퇴행 후보는 적용이 비활성화되고 `기각`할 수 있습니다. 평가는 가상 고정 사례 24건과 별도 회귀 사례 6건에 대한 실제 규칙 실행이며 실제 고객 성능이나 사람의 유용성 평가가 아닙니다.

LIVE LLM을 사용하려면 저장소 루트의 `.env.apikey`에 `openai_api_key`와 `openai_model`을 설정하고 워커를 재시작합니다. 해당 파일은 `.gitignore` 대상이며 이미지에 복사하지 않고 워커 환경에만 전달합니다. 키가 없거나 호출에 실패하면 실패 Run을 남기고 자동으로 MOCK으로 바꾸지 않습니다. 기본 모델 모드는 MOCK입니다.

## 테스트

아래 명령은 전용 컨테이너와 테스트 DB를 만듭니다. **테스트는 지정한 DB의 Work OS 테이블을 비웁니다. 운영 또는 보존할 데이터베이스를 지정하지 마세요.**

```powershell
docker run --name workos-kernel-test -e POSTGRES_PASSWORD=workos -e POSTGRES_USER=workos -e POSTGRES_DB=workos -p 55432:5432 -d postgres:16-alpine
docker run --name workos-redis-test -p 56379:6379 -d redis:7-alpine
python -m pip install -r backend/requirements.txt
$env:WORKOS_TEST_DATABASE_URL='postgresql+psycopg://workos:workos@localhost:55432/workos'
$env:WORKOS_TEST_REDIS_URL='redis://localhost:56379/0'
$env:WORKOS_TEST_OPENSEARCH_URL='http://localhost:9200'
python -m pytest -q tests
```

테스트 Redis 또는 OpenSearch URL을 생략하면 해당 통합 테스트는 건너뜁니다. OpenSearch 테스트는 고유한 임시 인덱스만 만들고 정리합니다. 테스트 후 `docker rm -f workos-kernel-test workos-redis-test`로 전용 컨테이너를 제거할 수 있습니다.

## 구현 경계

- PostgreSQL은 공식 상태와 이벤트, 작업 큐와 산출물 본문의 원본입니다. Redis Streams는 작업 알림을 전달하고, Redis 장애나 워커 중단 시 PostgreSQL 큐에서 복구합니다.
- TASK-001은 가상 지표와 문서의 SQL 스냅샷, 보고서, 수치·출처 Evidence 및 비교를 제공합니다. 금융 실데이터가 아니며 투자 의사결정 용도가 아닙니다. TASK-002의 규정도 가상이며 실제 법규 해석이 아닙니다. TASK-003의 후보는 데모용으로 미리 정한 분류 규칙이며 실제 VOC 유용성을 검증하지 않습니다.
- 테스트 전용 `mock_receipt` 검증기는 지정된 필드·모드·Task ID를 검사합니다. 실제 Task는 이 검증기를 사용하지 않으며 실제 업무 산출물을 발행하지 않습니다.
- TASK-001은 OpenSearch에 시드한 두 가상 문서를 ID·버전으로 검색하고 PostgreSQL 원본과 재검증합니다. TASK-002는 이전 정책 버전을 OpenSearch에서 조회하고 새 버전을 원본 저장소와 비교합니다. 새 버전의 인덱스 갱신 후속 작업은 아직 수행하지 않습니다. TASK-001과 TASK-003의 데이터 조회에는 로컬 읽기 전용 역할을 사용합니다. LangGraph 런타임은 아직 없고 파일 기반 또는 S3 저장소 대신 PostgreSQL 산출물 어댑터를 사용합니다.
