DROP TABLE IF EXISTS work_queue, work_dependencies, evidence CASCADE;
ALTER TABLE IF EXISTS work_items DROP CONSTRAINT IF EXISTS work_accepted_artifact_fk;
DROP TABLE IF EXISTS artifacts, runs, work_events, work_items, job_definitions, task_definitions CASCADE;
DROP FUNCTION IF EXISTS reject_event_mutation();
