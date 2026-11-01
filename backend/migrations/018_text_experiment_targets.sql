CREATE TABLE text_experiment_targets (
  experiment_id uuid PRIMARY KEY REFERENCES text_experiments(experiment_id),
  targets jsonb NOT NULL,
  targets_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER text_experiment_targets_immutable BEFORE UPDATE OR DELETE ON text_experiment_targets
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
