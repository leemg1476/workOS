CREATE TABLE work_designs (
  design_id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE work_design_revisions (
  design_id uuid NOT NULL REFERENCES work_designs(design_id),
  revision integer NOT NULL CHECK (revision > 0),
  description text NOT NULL, plan jsonb NOT NULL, origin text NOT NULL,
  model_id text, response_id text, feedback text, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(design_id,revision)
);
CREATE FUNCTION reject_work_design_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'design revisions are immutable'; END $$;
CREATE TRIGGER work_design_revision_immutable BEFORE UPDATE OR DELETE ON work_design_revisions FOR EACH ROW EXECUTE FUNCTION reject_work_design_revision_mutation();
