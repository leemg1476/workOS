-- Memory is a bounded projection of official history, pinned once per attempt.
CREATE TABLE run_task_memories (
  run_id uuid PRIMARY KEY REFERENCES runs(run_id),
  manifest jsonb NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER run_task_memories_immutable BEFORE UPDATE OR DELETE ON run_task_memories
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX task_memory_history ON work_items(task_id,task_version,created_at DESC);
