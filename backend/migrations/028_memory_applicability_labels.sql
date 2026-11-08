CREATE TABLE memory_applicability_labels (
  label_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(run_id), step_id text NOT NULL,
  snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER memory_applicability_label_immutable BEFORE UPDATE OR DELETE ON memory_applicability_labels
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX memory_applicability_latest ON memory_applicability_labels(run_id,step_id,created_at);
