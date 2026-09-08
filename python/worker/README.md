# worker

Consumes `papers.chunked` (full text) or `papers.new` (abstract-only fast path), embeds every chunk, optionally writes a summary, and upserts one paper per transaction. Bounded retries, then `papers.failed`.
