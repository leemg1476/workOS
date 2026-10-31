# Work OS — Codex Repository Instructions

## Mission

Build **Work OS v0.1** according to [init.md](init.md). This repository is a durable work-management runtime, not another chat-centric Agent dashboard.

Success is the ability to demonstrate three distinct workflows end to end:

1. TASK-001: scheduled financial briefing → versioned report → evidence → comparison and revalidation.
2. TASK-002: document-change event → dependency impact → follow-up WorkItem DAG.
3. TASK-003: VOC feedback → improvement proposal → champion/challenger evaluation → explicit apply/reject.

Treat these flows and their acceptance criteria in `init.md` as the current product contract. Do not silently change the scope.

## Product invariants

- **Work outlives a Run.** Persistent `TaskDefinition`, `JobDefinition` and `WorkItem` are separate from ephemeral agent executions.
- **Run is not the source of truth.** Store official Work state and immutable event history in PostgreSQL.
- **A model cannot verify itself by assertion.** Commit only after the configured validators pass; record Evidence and verifier versions.
- **Failed attempts remain inspectable.** A failed Run is not proof that its WorkItem goal is false.
- **Context is assembled, not inherited.** Form prompts from the active contract, verified dependencies, relevant sources, bounded failure history and available tools.
- **Data provenance is mandatory.** Persist document IDs/versions, SQL/data snapshots, artifact hashes, model/prompt/graph versions and evaluation IDs.
- **Improvement is not a new report.** Compare methods on the same fixed evaluation inputs. Require explicit user `Apply` before activating a challenger.
- **No fake integration.** Every UI result must identify MOCK or LIVE data; do not fabricate actual validation, model calls or score improvements.

## Scope and dependencies

- Primary stack: FastAPI, Pydantic, LangGraph, PostgreSQL, OpenSearch, Redis, Vue 3, Docker Compose; MinIO or a simple artifact-store adapter.
- Begin with local deterministic fixtures and a mock LLM adapter. Add live models/data only through clearly configured adapters.
- **Do not integrate AOC, external approval systems, GitLab or Langfuse.** Use local Run/Step logs and Work OS internal Task versioning.
- Demo is single-user. No IAM/SSO or external side-effecting operations (payments, deployments, real customer mutations).
- Prefer deterministic Python/SQL for counts, arithmetic, diffs and schema validation. Use LLM reasoning only where semantic work is required.

## How to work

1. Read this file and `init.md`; inspect existing code, status and tests before changing anything.
2. Identify the smallest useful vertical slice within P0 → P1 → P2; state assumptions and acceptance checks.
3. Implement one coherent slice, add or update tests, and run the narrowest relevant test suite.
4. Check that API schemas, DB migrations, UI labels and fixture data agree.
5. Demonstrate user-observable behavior, not just a passing unit test.
6. Update `docs/WORK_STATE.md` after each meaningful milestone with:
   - Current phase and completed acceptance checks
   - Paths/modules changed and decisions made
   - Commands actually run and their outcomes
   - Known blockers, schema/API invariants and the **next three concrete steps**
7. At the start of a new Codex session, read `docs/WORK_STATE.md` if present. Trust files, tests and database migrations over previous chat summaries.

Do not create placeholder implementations reported as finished. Preserve unrelated user changes. If network credentials or external privileges are missing, finish the MOCK path and report the unavailable LIVE functionality.

## Skill routing

Repository skills live in `.agents/skills/`. Load the matching skill before working in its area:

| Change/request | Skill |
|---|---|
| Data model, Work lifecycle, DAG, scheduler, event consistency | `workos-kernel` |
| SQL/OpenSearch connectors, source provenance, prompt context assembly | `workos-context` |
| TASK-001/002/003, seed data, API+UI demo flow | `workos-demo-workloads` |
| Evidence, evaluators, regression, improvement proposals, promotion/rollback | `workos-quality-loop` |

Read multiple skills only when the requested slice crosses their responsibilities. Avoid reading all skills for every small edit.

## Architecture boundaries

- `backend/app/domain`: pure contracts, state transitions, validation rules.
- `backend/app/api`: FastAPI HTTP interface with Pydantic contracts.
- `backend/app/work`: persistent Work coordination, events, dependencies and scheduling.
- `backend/app/runtime`: LangGraph and run-scoped tools; no direct mutation of official Work state.
- `backend/app/integrations`: read-only SQL, search, artifacts, optional LLM adapters.
- `backend/app/evaluation`: validators and champion/challenger comparison.
- `frontend/src`: Work-first UI; keep MOCK/LIVE status explicit.
- `tests` and `seeds`: reproducible fixtures and tests.

Adapt this layout if an existing codebase already has sound conventions; avoid a premature generalized framework.

## Verification / definition of done

- Test state transitions, retries/idempotency, DAG cycle prevention, stale propagation and artifact acceptance.
- Test 3 end-to-end demo flows against seeded data.
- A failed verification must not publish a successful artifact or erase a previous valid one.
- For improvement, compare Champion and Challenger on the same immutable evaluation suite, show regressions, and support rejection/rollback.
- Include the tests actually executed and remaining limitations in every handoff. Never report tests as passing unless executed.

## Implementation priority

P0: contracts, persistence, queue, local services, Work dashboard.

P1: the three complete demonstration paths with evidence and version history.

P2: real read-only data adapters, operational hardening and performance, only after P1 passes.

Do not broaden into marketplaces, global ontologies, model fine-tuning, multi-tenant billing or autonomous production changes.
