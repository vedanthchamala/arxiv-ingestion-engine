# syntax=docker/dockerfile:1
# Build context is the repo root: common/src/schema.rs embeds ../../../schemas/messages.v1.json.
# Targets: `poller`, `fetcher` (same runtime layers, different entrypoint).
#   docker build -f rust/Dockerfile --target fetcher -t arxiv-fetcher .
ARG RUST_VERSION=1.97

FROM rust:${RUST_VERSION}-bookworm AS builder
# cmake + C/C++ toolchain: librdkafka (rdkafka `cmake-build`) and aws-lc (rustls) compile from source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends cmake build-essential pkg-config zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY schemas/ schemas/
COPY rust/ rust/
WORKDIR /src/rust
# BuildKit cache mounts keep the crate registry and the incremental target/ between builds, so a
# source edit recompiles only the changed crates. The binaries are copied out because a cache
# mount is not part of the image layer.
RUN --mount=type=cache,id=arxiv-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=arxiv-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=arxiv-cargo-target,target=/src/rust/target,sharing=locked \
    cargo build --release --locked --workspace \
 && mkdir -p /out \
 && cp target/release/poller target/release/fetcher /out/ \
 && strip /out/poller /out/fetcher

FROM debian:bookworm-slim AS runtime
# ca-certificates: TLS to arxiv.org and huggingface.co (rustls). poppler-utils: `pdftotext` for the fetcher.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --user-group --create-home --home-dir /home/app app \
 && mkdir -p /hf && chown app:app /hf
# hf-hub caches the bge-m3 tokenizer under $HF_HOME/hub; mount a volume there so it downloads once.
ENV HF_HOME=/hf \
    RUST_LOG=info
COPY --from=builder /out/poller /out/fetcher /usr/local/bin/
USER app
WORKDIR /home/app

FROM runtime AS poller
ENTRYPOINT ["poller"]

FROM runtime AS fetcher
VOLUME /hf
ENTRYPOINT ["fetcher"]
