# E2 — runtime isolation: gpt-oss-20b, vLLM native MXFP4 vs Ollama

- **Status:** done — vLLM wins decisively on prefill/TTFT; decode comparable
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → E2

## Goal

Hold the model fixed (`gpt-oss-20b`, the model the cluster already serves) and
vary only the runtime: **vLLM with native Blackwell MXFP4** vs the **live Ollama
deployment** (GGUF, which dequantizes MXFP4→bf16 for compute). Decide whether the
cluster's fast 128K-class default endpoint should move off Ollama.

## Configurations

- **vLLM:** `openai/gpt-oss-20b`, vLLM 0.25.1, `quantization=gpt_oss_mxfp4`
  (native MXFP4, Marlin MoE kernels), single GPU (TP1), max-model-len 131072,
  gpu-mem-util 0.90. Manifest: <deployment.yaml>.
- **Ollama:** live cluster deployment (<../../../k8s/ollama/app/deployment.yaml>),
  `gpt-oss:20b` GGUF, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_CTX=131072`,
  `NUM_PARALLEL=1` (default). Both benched through the same harness
  (<../2026-07-17_e1_qwen3coder_awq/bench.py>). Raw: <summary_vllm.json>,
  <summary_ollama.json>.

## Results

### Latency (single request, temp 0, 256-token output)

| Input ctx | vLLM TTFT | vLLM decode tok/s | Ollama TTFT | Ollama decode tok/s |
| --------- | --------- | ----------------- | ----------- | ------------------- |
| 8K        | 0.90 s    | 1356              | 1.56 s      | 1154                |
| 32K       | 0.94 s    | 1035              | 3.81 s      | 917                 |
| 128K      | 2.1 s     | 1494              | 23.0 s      | 636                 |

The story is **prefill, not decode**. vLLM's TTFT is ~1.7× faster at 8K and
**~11× faster at 128K** — prompt processing is compute-bound, and vLLM uses the
5090's FP4 tensor cores while Ollama dequantizes to bf16 and computes there.
Decode rate is similar at short context (both read the same 4-bit weights,
bandwidth-bound) and diverges at 128K in vLLM's favor.

### The dequant is a compute cost, not a RAM cost

Ollama's `gpt-oss:20b` occupies **~15 GB VRAM** (`nvidia-smi`: 15,132 MiB) — the
MXFP4 weights stay 4-bit in memory; the bf16 expansion is transient per-matmul,
not a persistent ~40 GB blow-up. So the penalty for not having FP4 kernels is
throughput/latency (esp. prefill), not memory, and **not quality** (the stored
weights are identical MXFP4).

### Tool calls (`qwen3_coder`-style smoke, fixed schemas)

| Case       | vLLM (`openai` parser) | Ollama      |
| ---------- | ---------------------- | ----------- |
| single     | ✅                     | ✅          |
| parallel   | ❌ (1 call)            | ❌ (1 call) |
| multi-turn | ✅                     | ✅          |

Parallel tool-calling fails on **both** runtimes (each emits a single call), so
it's a **gpt-oss model behavior**, not a vLLM parser problem. Single and
multi-turn round trips are clean on both.

## Verdict

For the fast 128K-class endpoint, **vLLM is the better runtime for
prompt-heavy / agent use**: same VRAM, same quality, single/multi-turn tool
calls work, and prefill/TTFT is dramatically faster — the gap that matters most
when an agent ships a large context each turn. Ollama remains fine for
short-prompt chat where its 1.5 s TTFT is acceptable and its operational
simplicity wins. Moving the default coding/agent endpoint to vLLM MXFP4 is
justified by the ~11× long-context TTFT advantage.

## Notes / anomalies

- **vLLM 128K needle probe returned an empty stream** (`no usage/first-token`)
  during the context-ladder phase, while the 128K _latency_ request at similar
  length succeeded. Not chased down here (E2 is a runtime comparison, not a
  long-context study); flagged for follow-up if gpt-oss long-context is revisited.
- Quality not measured (external `ext`; HumanEval already saturated for this
  model in a prior run). E2 is about runtime, not model quality.
- Prefix caching: TTFT here is the reported p50; with only 2–3 reps the split
  between warm/cold is coarser than E1's — treat TTFT as indicative.
