-- Which figure-parser version produced each cached figure list; an older list is fetched and parsed again
-- (the first parser missed IEEE-style "Fig. 1:" captions).
ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS parser int NOT NULL DEFAULT 1;
