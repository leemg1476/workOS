# 2026-11-08 태스크 지식 수정 검증

목표는 최근 의견만 반복 소비하던 경로를 고쳐, 검토한 지식이 지속되고 계산·원문 근거가 실행과 평가에 실제 전달되도록 하는 것이다. 전문가 수준이나 50회 반복의 성능 향상을 입증한 실험은 아니다. 기존42회 연구와 점수는 그대로 두었으며 중단 상태를 보존한다.

| 문제 | 수정 | 확인 근거 |
|---|---|---|
| 최근 의견3개를 벗어나면 교훈 소실 | 별도의 불변 지식·결정 이력과 Run별 승인 지식 고정 | test_task_knowledge.py; LIVE에서 무관한 의견12개 이후에도 지식2개 유지 |
| 틀릴 수 있는 AI 비평이 다음 작성의 지시로 혼입 | AI 의견은 후보로 저장, 미승인 의견은 실제 작성 입력에서 제외 | live-proof.json의 prompt_memory와 제외 정책 |
| 같은 산술 오류 반복 | 제한된 Decimal AST와 백엔드 계산표 | 50,800.00원; 누락/잘못된 구조는 실패 Artifact로 보존하는 회귀 검사 |
| 요구한 항목 누락 | 승인 체크리스트 ID의 누락·중복·빈 본문 검사 | MOCK 실패/정상 및 LIVE 최종 JSON 확인 |
| 평가자가 원문을 받지 못함 | 실제 생성 후 원문 읽기와 해시, review-context | 공식 CPI 브리핑2개 CAPTURED; 역사적 Artifact는 수정하지 않음 |
| 입력/인용이 바뀌어도 재사용 | 정확한 입력 해시·버전 격리, 인용 불일치 시 STALE와 승인 차단 | 회귀 검사: 관련 Work만 무효화, 다른 입력 원문 사용 차단 |

실행 파일:

- [live-proof.json](live-proof.json): 별도 workos_knowledge_verify에서 합성 자료로 실제 gpt-6-luna 작성2회, Run/Work/컨텍스트/지식/프롬프트/검사 결과. Apply는 이 테스트 DB에서만 모의했다.
- [live-report-1.md](live-report-1.md), [live-report-2.md](live-report-2.md): 실제 두 보고서. 입력에 없는 B가구를 지어내지 않았고 백엔드 계산표를 포함한다. 문장의 유용성 점수를 매기지는 않았다.
- [historical-import-and-readback.json](historical-import-and-readback.json): preview의 기존 AI 의견45개 후보 수입과 공식8월/9월 원문2개 실제 읽기 결과. 이번 사후 readback을 과거 검색 원문으로 간주하지 않는다.
- [study-knowledge-proposals.json](study-knowledge-proposals.json): 계산7개·필수항목3개·원문인용1개의 새 후보 ID. 합계56개 후보, 적용0개. 기존 평가를 덮어쓰지 않는다.

검증 명령과 결과:

```powershell
# 별도 workos_memory_check DB 지정, PYTHONPATH=backend
# PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest -x -q --tb=short tests
# 115 passed, 6 skipped, 1 warning in 194.35s
npm --prefix frontend run build
# 성공
```

6개 skip은 OpenSearch/Redis 통합 URL 미설정이다. 새 지식 검사7개와 관련 좁은 검사24개도 실행했다. 마이그레이션024는 테스트 DB 임시 스키마에서 up/down/up을 통과했다. 실제 preview UI에서 계산 미리보기50,800원·2.54%·8,800원, B46,400원·2.32%·4,400원, 이자50,000원과 사유 없는 Apply 비활성화를 확인했다. API18018·worker는 새 코드로 재시작했다.

원래42회 연구 Task: USER-b884a865-8872-40f6-ba63-4883c541522c. [로컬 검토 화면](http://127.0.0.1:5178/?task=USER-b884a865-8872-40f6-ba63-4883c541522c). 이 환경은 기존 연구를 보존하는 preview DB이며 원래 Compose DB는 수정하지 않았다.

한계: 체크리스트 검사는 구조를, 직접 인용은 문자열 일치를, 계산은 주어진 입력의 연산을 확인한다. 본문 전체 의미·모든 숫자·인과 해석·출처의 신뢰성은 자동 보증하지 않는다. 원문 읽기는 HTML/plain만 지원하며 네트워크/PDF 실패를 숨기지 않는다. 모델 평가의 오류 가능성은 남는다. 다음 품질 판단은 별도 고정 입력·대조군·사람 검토로 해야 한다. 후보는 저장했지만 제품 규칙에 따른 사용자의 명시적 Apply 전에는 운영 지식으로 사용하지 않는다.
