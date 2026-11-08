CREATE TABLE experience_compile_inputs (
  work_id uuid PRIMARY KEY REFERENCES work_items(work_id), observation_id uuid NOT NULL REFERENCES experience_observations(observation_id),
  snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER experience_compile_input_immutable BEFORE UPDATE OR DELETE ON experience_compile_inputs
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
