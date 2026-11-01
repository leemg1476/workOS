# 업무 확장 경로

새 업무는 신뢰할 수 있는 Python 모듈에서 실행기와 독립 검증기를 등록할 수 있다. 예시는 [`example_total.py`](../backend/app/workloads/example_total.py)다. 기본적으로 로드하지 않는다.

1. 업무별 실행 함수 `execute(claim)`와 별도 검증 함수 `verify(conn, run, work, task, content, artifact_hash)`를 작성한다. 검증기는 입력 스냅샷·원천·산출물을 직접 확인하고 `(result, refs)`를 반환한다. `result.passed`는 명시적인 불리언이어야 한다. 모델이 작성한 통과 주장만으로 검증해서는 안 된다.
2. `register_executor(task_id, task_version, execute)`와 `register_verifier(kind, Verifier(...))`로 등록한다. 검증기에는 Artifact 종류·스키마 버전·검증기 버전·검증 종류·평가셋 버전을 지정한다. TaskDefinition의 `acceptance_policy.kind`는 등록한 kind와 일치해야 한다.
3. 해당 TaskDefinition과 JobDefinition을 버전 고정하여 별도의 마이그레이션 또는 시드로 배포한다. 실행기 키와 계약 버전이 일치해야 한다.
4. API·워커·예약 실행기 모두 같은 신뢰된 코드와 `WORKOS_WORKLOAD_MODULES` 설정을 사용해 배포한다. 쉼표로 구분한 Python 모듈을 불러온다. 예시: `WORKOS_WORKLOAD_MODULES=app.workloads.example_total`. 이 설정은 관리자 배포 설정이며 외부 요청이나 Task 데이터로부터 받지 않는다.
5. 정상 결과와 조작·누락 결과, 실패 이력, 재시도와 출처·의존성 변경을 테스트한다. 예시는 [`test_workload_extension.py`](../tests/test_workload_extension.py)를 참고한다.

실행기와 검증기를 업무 종류별로 공유하려면 `register_kind_executor(kind, execute)`를 사용할 수 있다. 이 실행기의 서명은 `execute(kernel, claim, contract)`다. 단일 업무가 하위 작업별 다른 검증기를 선택해야 하면 `register_verifier_kind_resolver(kind, resolver)`로 Work 스냅샷을 받아 검증기 kind를 반환한다. `register_acceptance_hook(kind, hook)`은 검증을 통과하고 채택된 뒤 같은 트랜잭션에서 `hook(kernel, conn, work, run_id)`를 호출한다. 후처리가 실패하면 채택·이벤트·산출물 저장도 함께 롤백된다. 실행기와 후처리에서는 공식 상태를 임의 수정하지 말고 커널의 전이·이벤트 경로를 사용한다.

작업 생성 시 입력 버전이나 외부 참조를 함께 고정하려면 `register_trigger_hook(kind, hook)`을 사용할 수 있다. `hook(kernel, conn, job, work_id)`는 Work 생성 트랜잭션 안에서 호출되며, 실패하면 작업과 이벤트가 함께 롤백된다. 사용자 텍스트 업무의 예시는 `text_sources.py`다. 외부 side effect를 후크에 넣거나 나중에 바뀌는 입력을 고정된 것처럼 취급해서는 안 된다.

업무의 재시도·재검증·사람 검토에 고유한 전제 조건이 있으면 각각 `register_retry_guard`, `register_revalidate_guard`, `register_review_guard`에 `guard(conn, work)`를 등록할 수 있다. 조건을 만족하지 못하면 `Conflict`를 발생시켜 트랜잭션을 중단한다. 정책 데모의 사람 검토 단계와 평가 전용 텍스트 실행이 예시다.

커널은 유효한 lease를 확인하고 트랜잭션 안에서 검증기를 실행하며 Artifact·Evidence·Evaluation과 상태 전이를 기록한다. 미등록 검증기는 거부하고 검증 실패는 `REWORK_REQUIRED`로 기록한다. 세 데모의 실행기·검증기·이벤트·검토·컨텍스트 구성은 업무 모듈에 있다. 범용 후속 DAG와 범용 이벤트 라우팅은 아직 자동 제공되지 않으며, 확장 실행기도 Graph 체크포인트와 Run 컨텍스트를 자동 제공하지 않는다. 플러그인 코드는 프로세스 내부에서 실행되며 격리된 샌드박스가 아니므로 신뢰된 코드만 배포해야 한다. 기본 제공 등록은 확장보다 먼저 로드되어 같은 이름의 중복 등록을 거부한다.
