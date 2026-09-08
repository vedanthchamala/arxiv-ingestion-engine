# Inference on the DGX Spark

Two vLLM servers on the Spark (aarch64, GB10, 121 GB unified memory, **shared with seven other
users**), OpenAI-compatible, bound to localhost and the Tailscale address only:

| Port | Purpose | Model | Pinned memory | Measured |
|---|---|---|---|---|
| 8001 | summaries (`/v1/chat/completions`) | `Qwen/Qwen2.5-7B-Instruct` | ~20 GB (`--gpu-memory-utilization 0.22`) | 13.8 tok/s per stream, up to 16 concurrent |
| 8002 | embeddings (`/v1/embeddings`, 1024-d) | `BAAI/bge-m3` | ~2 GB (`0.06`) | 32 × 300-token chunks in 0.36 s |

Image: `vllm/vllm-openai:cu130-nightly`, already on the box and proven on this GB10 (another user's
server runs it). Port 8000 belongs to that server; do not reuse it. Cold start to ready: ~150 s; the
first request after start is slow (warmup), then steady.

## Run
```
scp inference/spark/serve.sh spark:~/serve.sh
ssh spark '~/serve.sh up'        # down | status | logs [vllm-llm|vllm-embed]
ssh spark '~/serve.sh status'
```
Containers restart with the box (`--restart unless-stopped`); `~/serve.sh down` frees the memory.

## Point the Mac at it
Uncomment the Spark block in `.env` (or copy `.env.example` and swap the comments):
```
EMBEDDING_URL=http://<spark-tailscale-ip>:8002/v1
EMBEDDING_MODEL=BAAI/bge-m3
LLM_URL=http://<spark-tailscale-ip>:8001/v1
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
```
vLLM's and Ollama's `bge-m3` vectors agree to cosine 0.99999, so an index built with one can be
queried with the other. The Rust fetcher sizes chunks with the same tokenizer.

## Local fallback (Mac)
Ollama on `localhost:11434/v1` serves `bge-m3` and `qwen2.5:7b-instruct` with the identical
contract; that is what the default `.env.example` points at.
