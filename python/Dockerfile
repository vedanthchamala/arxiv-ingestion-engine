# syntax=docker/dockerfile:1
# Build context is the repo root so schemas/ can be copied in. Targets: `worker`, `api`.
#   docker build -f python/Dockerfile --target worker -t arxiv-worker .
# uv is pinned to the version that wrote python/uv.lock (and that satisfies the members'
# `uv_build` requirement natively); the floating ghcr.io/astral-sh/uv:python3.13-* tag lags it.
ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.2

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS base
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/python/.venv/bin:$PATH \
    ARXIV_SCHEMA_PATH=/app/schemas/messages.v1.json
WORKDIR /app/python

# 1. Third-party dependencies from the lockfile only; this layer survives source edits.
COPY python/pyproject.toml python/uv.lock python/.python-version ./
COPY python/arxiv_common/pyproject.toml python/arxiv_common/README.md arxiv_common/
COPY python/worker/pyproject.toml python/worker/README.md worker/
COPY python/query_api/pyproject.toml python/query_api/README.md query_api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages --no-install-workspace

# 2. Workspace members (editable, so sources stay under /app/python) and the shared schema.
COPY python/ ./
COPY schemas/ /app/schemas/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages \
 && python -m compileall -q arxiv_common/src worker/src query_api/src \
 && useradd --uid 10001 --user-group --create-home --home-dir /home/app app
USER app

FROM base AS worker
# Args come from compose `command:` (e.g. --topic papers.new --group worker-abstract --no-summary).
ENTRYPOINT ["arxiv-worker"]

FROM base AS api
EXPOSE 8000
CMD ["arxiv-query-api"]
