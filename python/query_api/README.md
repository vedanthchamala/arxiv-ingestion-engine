# query_api

FastAPI service. Web pages: `/` (latest papers, or search results with `q`), `/paper/{id}` (summary,
abstract, indexed sections, similar papers). JSON: `POST /search` (chunk-level ANN, best chunk per
paper, category and date filters, Redis semantic cache), `GET /papers/{id}`, `GET /papers/{id}/similar`,
`GET /stats`, `GET /health`, `GET /metrics`.
