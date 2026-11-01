CREATE TABLE text_contract_changes (
  change_id uuid PRIMARY KEY,
  task_id text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('APPLY','ROLLBACK')),
  note text NOT NULL CHECK (length(trim(note)) > 0),
  from_version integer NOT NULL,
  to_version integer NOT NULL,
  generation integer NOT NULL,
  from_plan_sha256 text NOT NULL,
  plan_sha256 text NOT NULL,
  targets jsonb NOT NULL,
  targets_sha256 text NOT NULL,
  reversed_change_id uuid REFERENCES text_contract_changes(change_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(task_id,from_version) REFERENCES task_definitions(task_id,version),
  FOREIGN KEY(task_id,to_version) REFERENCES task_definitions(task_id,version),
  CHECK ((decision='ROLLBACK') = (reversed_change_id IS NOT NULL))
);
CREATE UNIQUE INDEX text_contract_one_rollback ON text_contract_changes(reversed_change_id) WHERE reversed_change_id IS NOT NULL;
CREATE TRIGGER text_contract_changes_immutable BEFORE UPDATE OR DELETE ON text_contract_changes
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
