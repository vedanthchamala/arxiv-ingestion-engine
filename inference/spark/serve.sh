#!/usr/bin/env bash
# Run on the DGX Spark. Two vLLM containers behind OpenAI-compatible endpoints, bound to localhost
# and the Tailscale address only (the box is shared; port 8000 belongs to someone else):
#   :8001  chat completions (summaries)   MODEL_LLM
#   :8002  embeddings (1024-d)            MODEL_EMBED
# Usage: ./serve.sh [up|down|logs [name]|status]
set -euo pipefail

IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:cu130-nightly}"   # already on this box; works on GB10 (CUDA 13)
MODEL_LLM="${MODEL_LLM:-Qwen/Qwen2.5-7B-Instruct}"
MODEL_EMBED="${MODEL_EMBED:-BAAI/bge-m3}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface/hub}"
BIND="${BIND:-$(tailscale ip -4 2>/dev/null | head -1)}"   # this box's Tailscale address
# Fractions of the 121 GB unified pool. Shared machine: ~27 GB + ~7 GB, not the 0.8 NVIDIA suggests
# for a dedicated box. Raise if you own the machine.
LLM_MEM="${LLM_MEM:-0.22}"
EMBED_MEM="${EMBED_MEM:-0.06}"

common=(--gpus all --ipc host --ulimit memlock=-1 --ulimit stack=67108864 --entrypoint ""
        -e "HF_TOKEN=${HF_TOKEN:-}" -v "$HF_CACHE:/root/.cache/huggingface/hub" --restart unless-stopped)

up() {
  docker run -d --name vllm-llm "${common[@]}" -p 127.0.0.1:8001:8000 -p "$BIND:8001:8000" "$IMAGE" \
    vllm serve "$MODEL_LLM" --host 0.0.0.0 --port 8000 \
      --max-model-len 16384 --gpu-memory-utilization "$LLM_MEM" --dtype bfloat16 --max-num-seqs 16
  docker run -d --name vllm-embed "${common[@]}" -p 127.0.0.1:8002:8000 -p "$BIND:8002:8000" "$IMAGE" \
    vllm serve "$MODEL_EMBED" --host 0.0.0.0 --port 8000 --runner pooling \
      --max-model-len 8192 --gpu-memory-utilization "$EMBED_MEM"
  echo "started vllm-llm (:8001) and vllm-embed (:8002); first start downloads $MODEL_EMBED. Watch: $0 logs"
}
down()   { docker rm -f vllm-llm vllm-embed 2>/dev/null || true; }
logs()   { docker logs --tail 40 -f "${1:-vllm-llm}"; }
status() {
  docker ps --filter name=vllm- --format '{{.Names}}  {{.Status}}'
  for p in 8001 8002; do printf ':%s  ' "$p"; curl -sf "localhost:$p/v1/models" | head -c 160 || echo "(not ready)"; echo; done
}

case "${1:-up}" in
  up) up ;; down) down ;; logs) shift; logs "$@" ;; status) status ;;
  *) echo "usage: $0 [up|down|logs [name]|status]"; exit 1 ;;
esac
