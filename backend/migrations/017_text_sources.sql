CREATE TABLE text_source_versions (
 task_id text NOT NULL,
 version integer NOT NULL CHECK(version > 0),
 source_text text NOT NULL CHECK(length(trim(source_text)) > 0 AND length(source_text) <= 8000),
 source_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,version)
);
CREATE TABLE text_source_work (
 work_id uuid PRIMARY KEY REFERENCES work_items(work_id),
 task_id text NOT NULL,
 source_version integer NOT NULL,
 FOREIGN KEY(task_id,source_version) REFERENCES text_source_versions(task_id,version)
);
CREATE TRIGGER text_source_versions_immutable BEFORE UPDATE OR DELETE ON text_source_versions
 FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_source_work_immutable BEFORE UPDATE OR DELETE ON text_source_work
 FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
