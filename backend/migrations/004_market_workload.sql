CREATE TABLE market_observations (
  dataset_version text NOT NULL, symbol text NOT NULL, unit text NOT NULL,
  previous_value numeric NOT NULL, current_value numeric NOT NULL,
  observed_at timestamptz NOT NULL, PRIMARY KEY(dataset_version,symbol)
);
CREATE TABLE source_documents (
  document_id text NOT NULL, version integer NOT NULL, title text NOT NULL, body text NOT NULL,
  sha256 text NOT NULL, PRIMARY KEY(document_id,version)
);
CREATE TABLE run_contexts (
  run_id uuid PRIMARY KEY REFERENCES runs(run_id), manifest jsonb NOT NULL, sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evaluations (
  evaluation_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(run_id), artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  eval_suite_version text NOT NULL, metric_results jsonb NOT NULL, verdict text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reject_run_context_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'run context is immutable'; END $$;
CREATE TRIGGER run_context_immutable BEFORE UPDATE OR DELETE ON run_contexts FOR EACH ROW EXECUTE FUNCTION reject_run_context_mutation();
