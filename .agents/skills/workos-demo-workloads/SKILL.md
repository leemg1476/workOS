---
name: workos-demo-workloads
description: Implement and verify three Work OS MVP demos: scheduled financial-market briefing, policy-document change impact Work DAG, and VOC feedback-driven improvement recommendations. Use for demo fixtures, Job triggers, workflow APIs, Vue screens and TASK-001/002/003 end-to-end tests.
---

# Work OS Demo Workloads

Read `init.md` sections 2, 3, 6, 7 and 8 and the repository's `AGENTS.md`. Center the UI on Work outcomes rather than Agent directories or chat screens.

## Procedure

1. Create small deterministic fixtures: market observations, cited documents, policy versions v1/v2, anonymized VOC/feedback and a fixed evaluation set.
2. Build one complete Task from API → Job/Work → Run → Artifact → Evaluation → UI before generalizing.
3. TASK-001: run twice with an explicit market-data change; show two report versions, numeric delta, document refs and failed/recovered citation validation.
4. TASK-002: emit `document.updated` with stable event ID and old/new hashes; show changed clauses, affected Work, valid follow-up DAG and REVIEW_REQUIRED. Do not change operating rules.
5. TASK-003: produce VOC TOP 3 with cited record IDs, ingest negative feedback, create ImprovementProposal; use `workos-quality-loop` for comparison and apply/reject.
6. Wire Vue cards and detail views to real Work API responses. Display MOCK/LIVE and the latest Run/evaluation state explicitly.
7. Run seeded end-to-end tests and record actual outcomes in `docs/WORK_STATE.md`.

## Acceptance

- A user can trigger all three demos without private enterprise credentials.
- Every Task shows schedule/event, Run history, Artifact and verification result.
- Failed Run cause and recovery are inspectable.
- A changed source creates downstream Work, not just a written notification.
- A quality change appears as a versioned experiment, not a hidden prompt rewrite.

## Constraints

Use deterministic Python/SQL for counts, arithmetic, diffs and checks. Use LLMs for semantics with typed outputs. Reuse one Work Kernel, not three siloed stores. Do not add AOC, GitLab, Langfuse or separate approval systems.

## Output

Provide the demo UI paths, seed/reset commands, actual E2E test results, and a one-paragraph comparison of ordinary Agent output vs persistent Work execution.
