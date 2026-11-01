CREATE TABLE text_task_method_state (
  task_id text PRIMARY KEY,
  generation integer NOT NULL DEFAULT 0 CHECK(generation >= 0)
);
CREATE TABLE text_experiment_generation (
  experiment_id uuid PRIMARY KEY REFERENCES text_experiments(experiment_id),
  generation integer NOT NULL CHECK(generation >= 0)
);
INSERT INTO text_task_method_state(task_id)
  SELECT DISTINCT task_id FROM text_experiments;
INSERT INTO text_experiment_generation(experiment_id,generation)
  SELECT experiment_id,0 FROM text_experiments;
CREATE TABLE text_experiment_decisions (
  decision_id uuid PRIMARY KEY,
  experiment_id uuid NOT NULL REFERENCES text_experiments(experiment_id),
  decision text NOT NULL CHECK(decision IN ('APPLY','REJECT','ROLLBACK')),
  note text NOT NULL CHECK(length(trim(note)) > 0),
  from_version integer NOT NULL,
  to_version integer NOT NULL,
  generation integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(experiment_id,decision)
);
CREATE TRIGGER text_experiment_decisions_immutable BEFORE UPDATE OR DELETE ON text_experiment_decisions
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_experiment_generation_immutable BEFORE UPDATE OR DELETE ON text_experiment_generation
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
