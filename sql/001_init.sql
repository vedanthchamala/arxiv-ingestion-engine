CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS papers (
  arxiv_id         text        PRIMARY KEY,
  version          int         NOT NULL,
  title            text        NOT NULL,
  abstract         text        NOT NULL,
  primary_category text        NOT NULL,
  categories       text[]      NOT NULL,
  published_at     timestamptz NOT NULL,
  updated_at       timestamptz NOT NULL,
  pdf_url          text        NOT NULL,
  html_url         text,
  doi              text,
  journal_ref      text,
  comment          text,
  source           text,
  summary          text,
  summary_model    text,
  status           text        NOT NULL DEFAULT 'processed',
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  processed_at     timestamptz
);
CREATE INDEX IF NOT EXISTS papers_primary_category_idx ON papers (primary_category);
CREATE INDEX IF NOT EXISTS papers_categories_gin       ON papers USING gin (categories);
CREATE INDEX IF NOT EXISTS papers_published_at_idx     ON papers (published_at DESC);

CREATE TABLE IF NOT EXISTS authors (
  id        bigserial PRIMARY KEY,
  name      text NOT NULL,
  name_norm text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS paper_authors (
  arxiv_id  text   NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  author_id bigint NOT NULL REFERENCES authors(id)      ON DELETE CASCADE,
  position  int    NOT NULL,
  PRIMARY KEY (arxiv_id, author_id)
);
CREATE INDEX IF NOT EXISTS paper_authors_author_idx ON paper_authors (author_id);

CREATE TABLE IF NOT EXISTS chunks (
  id          text         PRIMARY KEY,
  arxiv_id    text         NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  idx         int          NOT NULL,
  section     text,
  text        text         NOT NULL,
  token_count int          NOT NULL,
  embedding   vector(1024) NOT NULL,
  UNIQUE (arxiv_id, idx)
);
CREATE INDEX IF NOT EXISTS chunks_arxiv_id_idx   ON chunks (arxiv_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
