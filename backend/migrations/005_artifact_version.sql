ALTER TABLE artifacts ADD COLUMN artifact_version integer;
CREATE UNIQUE INDEX artifact_kind_version ON artifacts(kind, artifact_version) WHERE artifact_version IS NOT NULL;
