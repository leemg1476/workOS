CREATE TABLE job_schedule_cursors (
  job_id text PRIMARY KEY REFERENCES job_definitions(job_id),
  next_due_at timestamptz NOT NULL
);
