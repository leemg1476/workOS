CREATE TABLE text_experiments (
  experiment_id uuid PRIMARY KEY,
  task_id text NOT NULL,
  champion_version integer NOT NULL,
  design_id uuid NOT NULL,
  candidate_revision integer NOT NULL,
  suite_id uuid NOT NULL REFERENCES text_regression_suites(suite_id),
  suite_sha256 text NOT NULL,
  champion_contract_sha256 text NOT NULL,
  candidate_plan jsonb NOT NULL,
  candidate_plan_sha256 text NOT NULL,
  status text NOT NULL CHECK (status IN ('RUNNING','EVALUATED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(task_id,champion_version) REFERENCES task_definitions(task_id,version),
  FOREIGN KEY(design_id,candidate_revision) REFERENCES work_design_revisions(design_id,revision)
);
CREATE TABLE text_experiment_work (
  experiment_id uuid NOT NULL REFERENCES text_experiments(experiment_id),
  case_id uuid NOT NULL REFERENCES text_regression_cases(case_id),
  side text NOT NULL CHECK (side IN ('CHAMPION','CHALLENGER')),
  work_id uuid NOT NULL UNIQUE REFERENCES work_items(work_id),
  PRIMARY KEY(experiment_id,case_id,side)
);
CREATE TABLE text_experiment_results (
  experiment_id uuid PRIMARY KEY REFERENCES text_experiments(experiment_id),
  result jsonb NOT NULL,
  result_sha256 text NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('PASS','FAIL')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION protect_text_experiment_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.task_id,NEW.champion_version,NEW.design_id,NEW.candidate_revision,NEW.suite_id,NEW.suite_sha256,NEW.champion_contract_sha256,NEW.candidate_plan,NEW.candidate_plan_sha256)
     IS DISTINCT FROM ROW(OLD.task_id,OLD.champion_version,OLD.design_id,OLD.candidate_revision,OLD.suite_id,OLD.suite_sha256,OLD.champion_contract_sha256,OLD.candidate_plan,OLD.candidate_plan_sha256) THEN
    RAISE EXCEPTION 'experiment snapshots are immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER text_experiment_snapshot_immutable BEFORE UPDATE ON text_experiments
  FOR EACH ROW EXECUTE FUNCTION protect_text_experiment_snapshot();
CREATE TRIGGER text_experiment_no_delete BEFORE DELETE ON text_experiments
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_experiment_results_immutable BEFORE UPDATE OR DELETE ON text_experiment_results
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER text_experiment_work_immutable BEFORE UPDATE OR DELETE ON text_experiment_work
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
