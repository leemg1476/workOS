DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM poc_voc_schedules) OR EXISTS (SELECT 1 FROM job_trigger_presets) THEN
    RAISE EXCEPTION 'Cannot downgrade while scheduled jobs or trigger presets exist';
  END IF;
END $$;
DROP TABLE poc_voc_schedules;
DROP TABLE job_trigger_presets;
