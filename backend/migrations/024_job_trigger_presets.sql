CREATE TABLE job_trigger_presets (
  job_id text PRIMARY KEY REFERENCES job_definitions(job_id),
  snapshot jsonb NOT NULL,
  snapshot_sha256 text NOT NULL,
  hidden_from_task_list boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER job_trigger_presets_immutable BEFORE UPDATE OR DELETE ON job_trigger_presets
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TABLE poc_voc_schedules (
  schedule_id uuid PRIMARY KEY,
  baseline_id uuid NOT NULL UNIQUE REFERENCES poc_voc_measurements(measurement_id),
  job_id text NOT NULL UNIQUE REFERENCES job_definitions(job_id),
  first_not_before timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER poc_voc_schedules_immutable BEFORE UPDATE OR DELETE ON poc_voc_schedules
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
