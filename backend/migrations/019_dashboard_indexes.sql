CREATE INDEX work_root_task_created_idx ON work_items(task_id,created_at DESC) WHERE parent_work_id IS NULL;
CREATE INDEX run_work_started_idx ON runs(work_id,started_at DESC,run_id DESC);
CREATE INDEX evaluation_run_created_idx ON evaluations(run_id,created_at DESC);
