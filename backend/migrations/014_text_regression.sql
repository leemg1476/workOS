CREATE TABLE text_regression_cases (
  case_id uuid PRIMARY KEY,
  task_id text NOT NULL,
  source_artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  review_evidence_id uuid NOT NULL REFERENCES evidence(evidence_id),
  snapshot jsonb NOT NULL,
  snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX text_cases_by_task ON text_regression_cases(task_id,created_at);
CREATE TRIGGER text_regression_cases_immutable BEFORE UPDATE OR DELETE ON text_regression_cases
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TABLE text_regression_suites (
  suite_id uuid PRIMARY KEY,
  task_id text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  snapshot jsonb NOT NULL,
  snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(task_id,version)
);
CREATE TRIGGER text_regression_suites_immutable BEFORE UPDATE OR DELETE ON text_regression_suites
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
