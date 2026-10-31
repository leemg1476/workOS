CREATE TABLE task_definitions (
  task_id text NOT NULL, version integer NOT NULL CHECK (version > 0), name text NOT NULL,
  objective text NOT NULL, input_schema jsonb NOT NULL, output_schema jsonb NOT NULL,
  acceptance_policy jsonb NOT NULL, active boolean NOT NULL DEFAULT false,
  PRIMARY KEY (task_id, version)
);
CREATE UNIQUE INDEX task_one_active ON task_definitions(task_id) WHERE active;
CREATE TABLE job_definitions (
  job_id text PRIMARY KEY, task_id text NOT NULL, task_version integer NOT NULL,
  trigger_type text NOT NULL CHECK (trigger_type IN ('MANUAL','CRON','EVENT')),
  schedule text, event_filter text, timezone text NOT NULL DEFAULT 'Asia/Seoul', enabled boolean NOT NULL DEFAULT true,
  FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version)
);
CREATE TABLE work_items (
  work_id uuid PRIMARY KEY, job_id text NOT NULL REFERENCES job_definitions(job_id),
  task_id text NOT NULL, task_version integer NOT NULL, input_snapshot jsonb NOT NULL,
  status text NOT NULL, parent_work_id uuid REFERENCES work_items(work_id), priority integer NOT NULL DEFAULT 0,
  version integer NOT NULL DEFAULT 1, accepted_artifact_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(), FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version)
);
CREATE TABLE runs (
  run_id uuid PRIMARY KEY, work_id uuid NOT NULL REFERENCES work_items(work_id), attempt_no integer NOT NULL,
  status text NOT NULL, agent_version text NOT NULL, model_id text NOT NULL, prompt_version text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, error text,
  UNIQUE(work_id,attempt_no)
);
CREATE UNIQUE INDEX runs_one_running ON runs(work_id) WHERE status = 'RUNNING';
CREATE TABLE artifacts (
  artifact_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(run_id), kind text NOT NULL,
  uri text NOT NULL, content jsonb NOT NULL, sha256 text NOT NULL, schema_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE work_items ADD CONSTRAINT work_accepted_artifact_fk FOREIGN KEY(accepted_artifact_id) REFERENCES artifacts(artifact_id);
CREATE TABLE evidence (
  evidence_id uuid PRIMARY KEY, artifact_id uuid NOT NULL REFERENCES artifacts(artifact_id),
  verification_type text NOT NULL, input_refs jsonb NOT NULL, result jsonb NOT NULL,
  verifier_version text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE work_dependencies (
  predecessor_id uuid NOT NULL REFERENCES work_items(work_id), successor_id uuid NOT NULL REFERENCES work_items(work_id),
  dependency_kind text NOT NULL, pinned_artifact_version uuid REFERENCES artifacts(artifact_id),
  PRIMARY KEY(predecessor_id,successor_id), CHECK (predecessor_id <> successor_id)
);
CREATE TABLE work_events (
  event_id uuid PRIMARY KEY, event_type text NOT NULL, subject_id text NOT NULL, causation_id text,
  actor text NOT NULL, prior_version integer, idempotency_key text UNIQUE, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reject_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'work_events is append-only'; END $$;
CREATE TRIGGER work_events_immutable BEFORE UPDATE OR DELETE ON work_events FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TABLE work_queue (
  work_id uuid PRIMARY KEY REFERENCES work_items(work_id), status text NOT NULL CHECK(status IN ('PENDING','CLAIMED','DONE')),
  available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz, run_id uuid REFERENCES runs(run_id)
);
CREATE INDEX queue_available ON work_queue(status,available_at);
