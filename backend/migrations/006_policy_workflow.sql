CREATE TABLE policy_documents (
  document_id text NOT NULL, version integer NOT NULL CHECK(version > 0), title text NOT NULL,
  clauses jsonb NOT NULL, sha256 text NOT NULL, PRIMARY KEY(document_id,version)
);
CREATE FUNCTION reject_policy_document_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'policy document versions are immutable'; END $$;
CREATE TRIGGER policy_document_immutable BEFORE UPDATE OR DELETE ON policy_documents FOR EACH ROW EXECUTE FUNCTION reject_policy_document_mutation();
CREATE TABLE task_source_dependencies (
  task_id text NOT NULL, document_id text NOT NULL, pinned_version integer NOT NULL,
  status text NOT NULL CHECK(status IN ('CURRENT','REVIEW_REQUIRED')),
  PRIMARY KEY(task_id,document_id), FOREIGN KEY(document_id,pinned_version) REFERENCES policy_documents(document_id,version)
);
CREATE TABLE policy_change_events (
  event_id text PRIMARY KEY, document_id text NOT NULL, old_version integer NOT NULL, new_version integer NOT NULL,
  old_sha256 text NOT NULL, new_sha256 text NOT NULL, affected_tasks jsonb NOT NULL,
  root_work_id uuid UNIQUE NOT NULL REFERENCES work_items(work_id), created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(document_id,old_version) REFERENCES policy_documents(document_id,version),
  FOREIGN KEY(document_id,new_version) REFERENCES policy_documents(document_id,version),
  UNIQUE(document_id,old_version,new_version)
);
CREATE TABLE policy_followups (
  root_work_id uuid NOT NULL REFERENCES work_items(work_id), work_id uuid UNIQUE NOT NULL REFERENCES work_items(work_id),
  step_no integer NOT NULL, title text NOT NULL, PRIMARY KEY(root_work_id,step_no)
);
CREATE TRIGGER policy_change_immutable BEFORE UPDATE OR DELETE ON policy_change_events FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
