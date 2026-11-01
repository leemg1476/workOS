DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM poc_voc_measurements) THEN
    RAISE EXCEPTION 'Cannot downgrade while human measurement evidence exists';
  END IF;
END $$;
DROP TABLE poc_voc_measurements;
