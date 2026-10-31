CREATE SCHEMA IF NOT EXISTS workos_checkpoints;
CREATE TABLE run_steps (
    step_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(run_id),
    graph_version text NOT NULL,
    node_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('STARTED', 'SUCCEEDED', 'FAILED')),
    error_type text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX run_steps_by_run ON run_steps(run_id, step_id);
CREATE TRIGGER run_steps_immutable BEFORE UPDATE OR DELETE ON run_steps
    FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
