-- Keyword side of hybrid search: title (weight A) and abstract (weight B), English stemming.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS tsv tsvector
  GENERATED ALWAYS AS (setweight(to_tsvector('english', title), 'A') ||
                       setweight(to_tsvector('english', abstract), 'B')) STORED;
CREATE INDEX IF NOT EXISTS papers_tsv_gin ON papers USING gin (tsv);
