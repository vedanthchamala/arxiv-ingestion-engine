-- Saved searches ("watches"): a query, optional category filter, and its embedding, so a watch
-- page or Atom feed never needs the inference server. seen_at marks the last visit (new = ingested since).
CREATE TABLE IF NOT EXISTS watches (
  id         bigserial    PRIMARY KEY,
  name       text         NOT NULL,
  query      text         NOT NULL,
  categories text[],
  embedding  vector(1024) NOT NULL,
  created_at timestamptz  NOT NULL DEFAULT now(),
  seen_at    timestamptz  NOT NULL DEFAULT now()
);
