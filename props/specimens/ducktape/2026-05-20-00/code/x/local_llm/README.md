# Local LLM scripts (wyrm2 host)

Runnable artifacts for local inference on wyrm2 (2× RTX 5090, 64 GB VRAM).
Analysis, comparisons, and lessons-learned have moved to the hub at
<../../cluster/docs/inference/>.

## Quick Start

```bash
cd ~/code/ducktape/x/local_llm

# Ollama (already running as systemd service on wyrm2)
ollama list
ollama run qwen3-coder-long

# vLLM with AWQ + FP8 KV cache (tensor parallel, 262K context)
./start-vllm-awq.sh
```

## OpenCode Integration

Both backends are configured in opencode. Select model in opencode UI:

- **Qwen3-Coder 30B 131k (local)** — Ollama backend
- **Qwen3-Coder 30B TP2 (vLLM)** — vLLM backend (must start server first)

Config: <../../nix/home/opencode/default.nix>

## Storage (host paths)

- Ollama models: `/wyrmhdd/ollama-models`
- HuggingFace cache: `/wyrmhdd/huggingface`

## Creating Ollama Model Variants

```bash
ollama create qwen3-coder-long -f Modelfile.qwen3-coder-long

# Or interactively
ollama run qwen3-coder:30b
/set parameter num_ctx 131072
/save qwen3-coder-long
/bye
```

## See also (hub)

- <../../cluster/docs/inference/README.md> — docs hub index
- <../../cluster/docs/inference/backend_comparison.md> — engine comparison + current state
- <../../cluster/docs/inference/vllm_history.md> — distilled vLLM lessons
- <../../cluster/docs/inference/qwen3_coder_vram_analysis.md> — full VRAM math + debug logs
- <../../cluster/docs/inference/model_download_history.md> — model search/download log
- <../../cluster/docs/inference/kv_cache_quantization.md> — KV cache dtype research
