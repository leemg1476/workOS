# 2025년 9월 가계 생활비 보고서: 42회 완료 품질평가

등록 Task: `USER-b884a865-8872-40f6-ba63-4883c541522c` · 실제 작성 모델: gpt-6-luna · 완료: 42/50회

분석 대상은 2025-09-01~2025-09-30으로 고정했다. 매회 웹 자료를 추가 탐색하고 이전 초안·AI 피드백을 태스크 기억에서 재사용했다. 점수는 같은 모델의 별도 요청으로 고정 루브릭을 적용한 참고 평가이며, 사람의 전문가 평가나 인과적 학습효과의 증명이 아니다.

| 회차 | AI 점수 /100 | AI 중대오류 경고(미확정) | 본문 출처 URL | 최근 피드백/초안 기억 | 원문 |
|---:|---:|---:|---:|---:|---|
| 1 | 60 | 1 | 1 | 0/0 | [보고서](round-01/report.md) · [평가](round-01/grade.json) |
| 10 | 83 | 0 | 3 | 3/2 | [보고서](round-10/report.md) · [평가](round-10/grade.json) |
| 30 | 83 | 0 | 3 | 3/2 | [보고서](round-30/report.md) · [평가](round-30/grade.json) |
| 42 | 85 | 0 | 4 | 3/2 | [보고서](round-42/report.md) · [평가](round-42/grade.json) |
| 50 | 미완료 | — | — | — | — |

## 항목별 평가(0~5)

| 항목 / 배점 | 1회차 | 10회차 | 30회차 | 50회차 |
|---|---:|---:|---:|---:|
| facts / 20 | 4 | 4 | 4 | — |
| arithmetic / 20 | 2 | 5 | 5 | — |
| explanation / 15 | 3 | 4 | 4 | — |
| households / 10 | 3 | 5 | 5 | — |
| actions / 15 | 2 | 3 | 3 | — |
| readability / 10 | 4 | 4 | 4 | — |
| sources / 5 | 4 | 4 | 4 | — |
| uncertainty / 5 | 3 | 4 | 4 | — |

## 해석의 경계

- one adaptive trajectory on one fixed scenario
- same-model automated judge; no human calibration
- additional web information and feedback co-vary; not a causal memory ablation
- source URLs and provider search provenance are not full immutable page snapshots
- high rubric scores do not prove expert human parity or generalization

실제 본문의 원시 URL 29개, 추적 파라미터와 같은 기사·파일의 경로 중복을 정규화한 인용 대상 13개. 검색 결과 URL 304개는 본문에서 인용·이해했다는 뜻이 아니다. 정규화는 문서 내용의 독립성이나 정확성을 검증하지 않으며 출처 개수를 품질 점수로 환산하지 않았다.

산술 기준은 [고정 계산 기준](arithmetic_reference.json), 평가 기준은 [루브릭](rubric.json), 검색 출처의 최초 본문 등장은 [출처 장부](source_ledger.json), 전 회차 지표와 해시는 [원본 집계](summary.json)에 보존했다.