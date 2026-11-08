-- Fail closed if normalized rules exist; export/restore them before downgrading.
ALTER TABLE task_knowledge DROP CONSTRAINT task_knowledge_kind_check;
ALTER TABLE task_knowledge ADD CONSTRAINT task_knowledge_kind_check
  CHECK(kind IN ('LESSON','SOURCE_EXCERPT','CALCULATION','CHECKLIST'));
DROP TABLE memory_application_traces, experience_observations, knowledge_relations, task_procedure_bindings;
