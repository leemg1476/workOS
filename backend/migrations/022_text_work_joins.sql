CREATE TABLE text_work_joins (
  join_id uuid PRIMARY KEY,
  target_task_id text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  version integer NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE text_work_join_sources (
  join_id uuid NOT NULL REFERENCES text_work_joins(join_id),
  source_task_id text NOT NULL,
  PRIMARY KEY(join_id,source_task_id)
);
CREATE TABLE text_work_join_arrivals (
  arrival_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  join_id uuid NOT NULL REFERENCES text_work_joins(join_id),
  join_version integer NOT NULL,
  source_task_id text NOT NULL,
  source_work_id uuid NOT NULL REFERENCES work_items(work_id),
  source_artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  source_artifact_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(join_id,source_task_id) REFERENCES text_work_join_sources(join_id,source_task_id),
  UNIQUE(join_id,join_version,source_artifact_id)
);
CREATE INDEX text_join_arrival_lookup ON text_work_join_arrivals(join_id,join_version,source_task_id,arrival_id DESC);
CREATE TABLE text_work_join_firings (
  firing_id uuid PRIMARY KEY,
  join_id uuid NOT NULL REFERENCES text_work_joins(join_id),
  join_version integer NOT NULL,
  target_work_id uuid NOT NULL UNIQUE REFERENCES work_items(work_id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE text_work_join_firing_inputs (
  firing_id uuid NOT NULL REFERENCES text_work_join_firings(firing_id),
  arrival_id bigint NOT NULL REFERENCES text_work_join_arrivals(arrival_id),
  PRIMARY KEY(firing_id,arrival_id),
  UNIQUE(arrival_id)
);
CREATE TRIGGER text_join_sources_immutable BEFORE UPDATE OR DELETE ON text_work_join_sources FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_join_arrivals_immutable BEFORE UPDATE OR DELETE ON text_work_join_arrivals FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_join_firings_immutable BEFORE UPDATE OR DELETE ON text_work_join_firings FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_join_firing_inputs_immutable BEFORE UPDATE OR DELETE ON text_work_join_firing_inputs FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
