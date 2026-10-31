CREATE TABLE voc_records (
  record_id text PRIMARY KEY, source_version text NOT NULL, body text NOT NULL
);
CREATE TABLE voc_method_versions (
  method_id text PRIMARY KEY, changed_component text NOT NULL, rules jsonb NOT NULL, sha256 text NOT NULL
);
CREATE TABLE voc_method_state (
  task_id text PRIMARY KEY, active_method_id text NOT NULL REFERENCES voc_method_versions(method_id),
  version integer NOT NULL DEFAULT 1, last_proposal_id uuid
);
CREATE TABLE voc_eval_suites (
  suite_version text PRIMARY KEY, case_count integer NOT NULL, sha256 text NOT NULL
);
CREATE TABLE voc_eval_cases (
  suite_version text NOT NULL REFERENCES voc_eval_suites(suite_version), case_id text NOT NULL,
  body text NOT NULL, expected_category text NOT NULL, PRIMARY KEY(suite_version,case_id)
);
CREATE TABLE voc_feedback (
  feedback_id uuid PRIMARY KEY, artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  recommendation_id text NOT NULL, reason text NOT NULL, idempotency_key text UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE improvement_proposals (
  proposal_id uuid PRIMARY KEY, feedback_id uuid NOT NULL REFERENCES voc_feedback(feedback_id),
  task_id text NOT NULL, changed_component text NOT NULL, champion_version text NOT NULL REFERENCES voc_method_versions(method_id),
  challenger_version text NOT NULL REFERENCES voc_method_versions(method_id), suite_version text NOT NULL REFERENCES voc_eval_suites(suite_version),
  evaluation_id uuid, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE improvement_evaluations (
  evaluation_id uuid PRIMARY KEY, proposal_id uuid NOT NULL REFERENCES improvement_proposals(proposal_id),
  suite_version text NOT NULL, suite_sha256 text NOT NULL, champion_sha256 text NOT NULL, challenger_sha256 text NOT NULL,
  results jsonb NOT NULL, verdict text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE improvement_proposals ADD CONSTRAINT improvement_evaluation_fk FOREIGN KEY(evaluation_id) REFERENCES improvement_evaluations(evaluation_id);
CREATE TRIGGER immutable_voc_records BEFORE UPDATE OR DELETE ON voc_records FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER immutable_voc_methods BEFORE UPDATE OR DELETE ON voc_method_versions FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER immutable_voc_suites BEFORE UPDATE OR DELETE ON voc_eval_suites FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER immutable_voc_eval_cases BEFORE UPDATE OR DELETE ON voc_eval_cases FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER immutable_voc_feedback BEFORE UPDATE OR DELETE ON voc_feedback FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER immutable_improvement_evaluations BEFORE UPDATE OR DELETE ON improvement_evaluations FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
