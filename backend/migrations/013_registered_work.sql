CREATE TABLE registered_work_designs (
 design_id uuid NOT NULL,
 revision integer NOT NULL,
 task_id text NOT NULL,
 task_version integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(design_id,revision),
 UNIQUE(task_id,task_version),
 FOREIGN KEY(design_id,revision) REFERENCES work_design_revisions(design_id,revision),
 FOREIGN KEY(task_id,task_version) REFERENCES task_definitions(task_id,version)
);
