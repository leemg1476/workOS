ALTER TABLE task_knowledge DROP CONSTRAINT task_knowledge_kind_check;
ALTER TABLE task_knowledge ADD CONSTRAINT task_knowledge_kind_check
  CHECK(kind IN ('LESSON','SOURCE_EXCERPT','CALCULATION','CHECKLIST','RULE'));
CREATE TABLE task_procedure_bindings (
  task_id text NOT NULL, task_version integer NOT NULL, binding jsonb NOT NULL,
  binding_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(task_id,task_version),
  FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version)
);
CREATE TABLE knowledge_relations (
  relation_id uuid PRIMARY KEY, entry_id uuid NOT NULL REFERENCES task_knowledge(entry_id),
  target_entry_id uuid NOT NULL REFERENCES task_knowledge(entry_id),
  kind text NOT NULL CHECK(kind IN ('DERIVED_FROM','DUPLICATE_OF','CONFLICTS_WITH','SUPERSEDES','ENRICHES')),
  note text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  CHECK(entry_id<>target_entry_id), UNIQUE(entry_id,target_entry_id,kind)
);
CREATE TABLE experience_observations (
  observation_id uuid PRIMARY KEY, task_id text NOT NULL, task_version integer NOT NULL,
  artifact_id uuid REFERENCES artifacts(artifact_id), event_id uuid REFERENCES work_events(event_id),
  classification text NOT NULL, snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version)
);
CREATE TABLE memory_application_traces (
  trace_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(run_id), step_id text NOT NULL,
  phase text NOT NULL CHECK(phase IN ('SELECT','VERIFY')), snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(run_id,step_id,phase)
);
CREATE TRIGGER task_procedure_binding_immutable BEFORE UPDATE OR DELETE ON task_procedure_bindings
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER knowledge_relation_immutable BEFORE UPDATE OR DELETE ON knowledge_relations
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER experience_observation_immutable BEFORE UPDATE OR DELETE ON experience_observations
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE TRIGGER memory_application_trace_immutable BEFORE UPDATE OR DELETE ON memory_application_traces
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX knowledge_relations_target ON knowledge_relations(target_entry_id,kind);
CREATE INDEX memory_application_run ON memory_application_traces(run_id,phase);
