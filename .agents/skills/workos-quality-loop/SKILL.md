---
name: workos-quality-loop
description: Design and implement Work OS evidence checks, fixed regression datasets, VOC feedback, champion/challenger evaluations, improvement proposals and version apply/reject/rollback. Use for verification, scoring, continual improvement and acceptance gates.
---

# Work OS Quality and Improvement Loop

Read `init.md` sections 4, 5, 6 and TASK-003 and the repository's `AGENTS.md`.

## Procedure

1. Specify measurable Task acceptance criteria before adding an LLM judge: schema, numeric correctness, citations, freshness and task-specific rubrics.
2. Execute deterministic validators first. Link each verdict to immutable Artifact, source version, verifier version and evaluation-suite version.
3. For semantic checks, record rubric, judge model/version, rationale summary and uncertainty. Calibrate against human-labeled samples; never treat an LLM judge as ground truth.
4. Turn negative feedback into reproducible evaluation cases after masking personal information. A complaint is evidence to investigate, not automatically a verified failure.
5. Propose a single-component change per ImprovementProposal: prompt, retrieval, plan/graph, connector/tool or validator. Never let a candidate weaken its own acceptance tests.
6. Replay Champion and Challenger on identical fixed evaluation inputs and source snapshots. Compare quality, hard-gate failures, regressions, latency and cost.
7. Keep Champion active until tests pass and the user explicitly clicks Apply. Support Reject and rollback, preserving both prior versions.
8. Test degraded challengers, missing citations, ties, judge disagreements, stale suites and rollback.

## Rules

- A report regenerated on newer data is not a method improvement.
- A single aggregate score must not hide a critical violation.
- Do not auto-promote because a model claims improvement.
- Preserve failed/rejected experiments and all prior accepted Evidence.
- Version the evaluation set separately from candidate behavior.
- TASK-003 must demonstrate both a passing and a rejected candidate; a rejected MOCK fixture is acceptable if labeled.

## Output

Demonstrate feedback → proposal → changed component → paired evaluation → user Apply/Reject → next Run using selected version. Include score breakdown, evidence links, tested rollback and observed test results.
