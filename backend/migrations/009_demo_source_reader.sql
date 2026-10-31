DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workos_demo_reader') THEN
    CREATE ROLE workos_demo_reader LOGIN PASSWORD 'workos_demo_reader';
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO workos_demo_reader;
GRANT SELECT ON market_observations, source_documents, voc_records TO workos_demo_reader;
