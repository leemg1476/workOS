CREATE TABLE report_source_snapshots (
  snapshot_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(run_id),
  url text NOT NULL, text_content text NOT NULL, text_sha256 text NOT NULL,
  content_type text NOT NULL, captured_at timestamptz NOT NULL DEFAULT now(),
  scope text NOT NULL DEFAULT 'POST_GENERATION_SOURCE_READBACK_NOT_FACT_VERIFICATION'
);
CREATE TRIGGER report_source_snapshot_immutable BEFORE UPDATE OR DELETE ON report_source_snapshots
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX report_sources_run ON report_source_snapshots(run_id);
CREATE TABLE task_knowledge (
  entry_id uuid PRIMARY KEY, task_id text NOT NULL, task_version integer NOT NULL,
  kind text NOT NULL CHECK(kind IN ('LESSON','SOURCE_EXCERPT','CALCULATION','CHECKLIST')),
  title text NOT NULL, payload jsonb NOT NULL, source_scope_sha256 text NOT NULL,
  origin_artifact_id uuid REFERENCES artifacts(artifact_id), sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version),
  UNIQUE(task_id,task_version,sha256)
);
CREATE TABLE task_knowledge_decisions (
  decision_id uuid PRIMARY KEY, entry_id uuid NOT NULL REFERENCES task_knowledge(entry_id),
  decision text NOT NULL CHECK(decision IN ('APPLY','REJECT','RETIRE')),
  note text NOT NULL, actor text NOT NULL CHECK(actor='user'), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER task_knowledge_immutable BEFORE UPDATE OR DELETE ON task_knowledge
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER task_knowledge_decision_immutable BEFORE UPDATE OR DELETE ON task_knowledge_decisions
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX task_knowledge_scope ON task_knowledge(task_id,task_version,source_scope_sha256);
CREATE INDEX knowledge_decision_latest ON task_knowledge_decisions(entry_id,created_at DESC);
