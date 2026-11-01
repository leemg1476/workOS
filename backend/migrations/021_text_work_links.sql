CREATE TABLE text_work_links (
  link_id uuid PRIMARY KEY,
  source_task_id text NOT NULL,
  target_task_id text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_task_id,target_task_id),
  CHECK (source_task_id <> target_task_id)
);
CREATE TABLE text_work_link_deliveries (
  delivery_id uuid PRIMARY KEY,
  link_id uuid NOT NULL REFERENCES text_work_links(link_id),
  link_version integer NOT NULL,
  source_work_id uuid NOT NULL REFERENCES work_items(work_id),
  source_artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  source_artifact_sha256 text NOT NULL,
  target_work_id uuid NOT NULL UNIQUE REFERENCES work_items(work_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(link_id,source_artifact_id)
);
CREATE TRIGGER text_work_link_deliveries_immutable BEFORE UPDATE OR DELETE ON text_work_link_deliveries
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
