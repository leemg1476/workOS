CREATE TABLE poc_voc_measurements (
  measurement_id uuid PRIMARY KEY,
  kind text NOT NULL CHECK(kind IN ('BASELINE','ASSISTED')),
  period text NOT NULL CHECK(period IN ('week1','week2')),
  baseline_id uuid REFERENCES poc_voc_measurements(measurement_id),
  run_id uuid REFERENCES runs(run_id),
  human_seconds integer NOT NULL CHECK(human_seconds BETWEEN 1 AND 86400),
  time_basis text NOT NULL CHECK(time_basis='SELF_REPORTED'),
  answers jsonb NOT NULL,
  reviewed_ids jsonb NOT NULL,
  snapshot jsonb NOT NULL,
  snapshot_sha256 text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind='BASELINE' AND period='week1' AND baseline_id IS NULL AND run_id IS NULL)
      OR (kind='ASSISTED' AND baseline_id IS NOT NULL AND run_id IS NOT NULL))
);
CREATE TRIGGER poc_voc_measurements_immutable BEFORE UPDATE OR DELETE ON poc_voc_measurements
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
