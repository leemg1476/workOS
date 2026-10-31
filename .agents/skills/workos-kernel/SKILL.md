---
name: workos-kernel
description: Implement or review Work OS persistent TaskDefinition, JobDefinition, WorkItem, Run and Event models, lifecycle transitions, dependency DAG, scheduling, concurrency, retry and stale propagation. Use when changing the kernel schema, persistence, state machine, event handling or scheduler.
---

# Work OS Kernel

Read `init.md` sections 1, 3, 4 and 8 and the repository's `AGENTS.md` before making kernel changes.

## Procedure

1. Identify the requested behavior and which contract is affected: TaskDefinition, JobDefinition, WorkItem, Run, Event or Dependency.
2. Specify the state transition or database invariant first. Distinguish a reusable Task version from a recurring Job and a specific Work execution.
3. Update typed Pydantic contracts and PostgreSQL migrations together. Preserve existing data; make migrations reversible where practical.
4. Implement transitions in one domain service. Agent nodes and HTTP handlers must call it instead of writing arbitrary statuses.
5. Append an event in the same transaction as the official state change. Capture actor, cause, prior version and idempotency key.
6. Execute Job triggers through a durable queue. Repeated delivery of the same trigger must not create duplicate WorkItems. A retry creates a new Run, not a new Work identity.
7. Maintain a DAG of dependencies; reject cycles and recalculate only affected descendants when input or accepted evidence becomes stale.
8. Add unit tests for valid/invalid transitions, duplicate events, concurrent claims, retry, stale propagation and dependency version changes.
9. Update `docs/WORK_STATE.md` with verified migrations, test results and next steps.

## Invariants

- Never mark Work COMMITTED from the model's own assertion.
- Keep execution status and Work acceptance status distinct.
- Preserve failed Runs and previous accepted Artifacts.
- Never use an embedding index or chat memory as the canonical Work database.
- Do not add AOC, GitLab, Langfuse or a separate approval service.
- For external exactly-once side effects, reconcile the external system's record and use idempotency keys.

## Output

Report the schema/contracts changed, transition rules, one runnable test path and observed results. Defer any abstraction not needed by TASK-001/002/003.
