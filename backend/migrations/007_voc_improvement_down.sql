ALTER TABLE IF EXISTS improvement_proposals DROP CONSTRAINT IF EXISTS improvement_evaluation_fk;
DROP TABLE IF EXISTS improvement_evaluations, improvement_proposals, voc_feedback, voc_eval_cases, voc_eval_suites, voc_method_state, voc_method_versions, voc_records CASCADE;
