CREATE TABLE memory_comparison_studies (
  study_id uuid PRIMARY KEY, snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE memory_study_learning (
  learning_id uuid PRIMARY KEY, study_id uuid NOT NULL REFERENCES memory_comparison_studies(study_id),
  arm text NOT NULL CHECK(arm IN ('B','C')), round_no integer NOT NULL,
  snapshot jsonb NOT NULL,snapshot_sha256 text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(study_id,arm,round_no)
);
CREATE TABLE memory_study_work (
  work_id uuid PRIMARY KEY REFERENCES work_items(work_id), study_id uuid NOT NULL REFERENCES memory_comparison_studies(study_id),
  arm text NOT NULL CHECK(arm IN ('A','B','C')), phase text NOT NULL CHECK(phase IN ('TRAIN','HELDOUT')),
  case_id text NOT NULL, snapshot jsonb NOT NULL,snapshot_sha256 text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(study_id,arm,phase,case_id)
);
CREATE TRIGGER memory_study_immutable BEFORE UPDATE OR DELETE ON memory_comparison_studies
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER memory_learning_immutable BEFORE UPDATE OR DELETE ON memory_study_learning
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER memory_study_work_immutable BEFORE UPDATE OR DELETE ON memory_study_work
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
