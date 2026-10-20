-- High-level design walkthroughs, generated on first view and cached per paper and provider (so a local
-- model's version and Claude's can be compared side by side), and the figures of each paper's arXiv HTML.
CREATE TABLE IF NOT EXISTS paper_designs (
  arxiv_id       text         NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  provider       text         NOT NULL,              -- local | claude
  version        int          NOT NULL,              -- paper version the design was written from
  model          text         NOT NULL,
  prompt_version int          NOT NULL,
  status         text         NOT NULL,              -- running | done | failed
  design         jsonb,
  rejected       jsonb        NOT NULL DEFAULT '[]', -- claims the validator removed, with the reason
  error          text,
  input_tokens   int,
  output_tokens  int,
  cost_usd       numeric(10, 5),
  seconds        real,
  started_at     timestamptz  NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  PRIMARY KEY (arxiv_id, provider)
);

CREATE TABLE IF NOT EXISTS paper_figures (
  arxiv_id   text        PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  version    int         NOT NULL,
  status     text        NOT NULL,                   -- done | none (no HTML rendering) | failed
  figures    jsonb       NOT NULL DEFAULT '[]',      -- [{n, caption, images: [url, ...]}]
  error      text,
  fetched_at timestamptz NOT NULL DEFAULT now()
);
