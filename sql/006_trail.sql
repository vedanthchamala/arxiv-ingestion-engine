-- Reading trail ("webs"): the papers a reader actually read, grouped into one web per research area, and
-- the citation edges that let suggestions say "builds on" / "followed by" (decision 38).
CREATE TABLE IF NOT EXISTS webs (
  id         bigserial   PRIMARY KEY,
  name       text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per paper on the reader's path; a paper belongs to exactly one web.
CREATE TABLE IF NOT EXISTS trail (
  arxiv_id     text        PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  web_id       bigint      NOT NULL REFERENCES webs(id) ON DELETE CASCADE,
  added_at     timestamptz NOT NULL DEFAULT now(),
  via_arxiv_id text,       -- the path paper a suggestion branched from
  via_kind     text,       -- builds_on | followed_by | similar | search | manual
  query        text        -- the search that led to it, if any
);
CREATE INDEX IF NOT EXISTS trail_web_idx ON trail (web_id, added_at);

-- Suggestions the reader said no to: an arXiv id or "s2:<paperId>" for papers outside the index.
CREATE TABLE IF NOT EXISTS dismissed (
  key text        PRIMARY KEY,
  at  timestamptz NOT NULL DEFAULT now()
);

-- Citation edges from Semantic Scholar (or the arXiv HTML bibliography as a fallback).
-- direction 'cites': arxiv_id's bibliography lists other; 'cited_by': other's bibliography lists arxiv_id.
CREATE TABLE IF NOT EXISTS citations (
  arxiv_id       text NOT NULL,
  direction      text NOT NULL,
  other_key      text NOT NULL,   -- "arxiv:<id>" or "s2:<paperId>"
  other_arxiv_id text,
  title          text,
  year           int,
  PRIMARY KEY (arxiv_id, direction, other_key)
);
CREATE INDEX IF NOT EXISTS citations_other_idx ON citations (other_arxiv_id);

CREATE TABLE IF NOT EXISTS citation_fetches (
  arxiv_id   text        PRIMARY KEY,
  source     text        NOT NULL,  -- s2 | html
  status     text        NOT NULL,  -- done | missing | failed
  refs       int,
  cites      int,
  error      text,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

-- Paper-level neighbours use only chunk 0 (title + abstract); a partial HNSW index keeps that fast.
CREATE INDEX IF NOT EXISTS chunks_abstract_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WHERE idx = 0;
