CREATE TABLE text_experiment_knowledge (
  experiment_id uuid PRIMARY KEY REFERENCES text_experiments(experiment_id),
  candidate_entry_id uuid NOT NULL REFERENCES task_knowledge(entry_id),
  snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER text_experiment_knowledge_immutable BEFORE UPDATE OR DELETE ON text_experiment_knowledge
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX knowledge_experiment_candidate ON text_experiment_knowledge(candidate_entry_id);
