CREATE TABLE knowledge_semantic_reviews (
  review_id uuid PRIMARY KEY, experiment_id uuid NOT NULL REFERENCES text_experiments(experiment_id),
  packet_sha256 text NOT NULL, snapshot jsonb NOT NULL, snapshot_sha256 text NOT NULL,
  verdict text NOT NULL CHECK(verdict IN ('PASS','REJECT')), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER knowledge_semantic_review_immutable BEFORE UPDATE OR DELETE ON knowledge_semantic_reviews
  FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
CREATE INDEX knowledge_semantic_review_latest ON knowledge_semantic_reviews(experiment_id,created_at);
