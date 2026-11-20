# Inference backends

## Goal

Two RTX 5090s on `wyrm2`. The point of this directory is to figure out
**how to make them do useful work for me** — and to keep doing so as
models, configs, and tasks evolve. Three things to decide:

1. **Which models** — quality on jobs that matter (AI-powered coding;
   reverse engineering, see <../../../devinfra/skills/reverse_engineer/>; FreeCAD,
   see <../../../skills/freecad/>; reasoning tasks generally).
2. **Which inference config** — backend (Ollama, vLLM, …), quant,
   parallelism, context — for speed and usability.
3. **Which jobs / proxies** — usually off-the-shelf Inspect AI tasks
   (AIME, HumanEval+, GPQA, …) since most things we care about have a
   close-enough proxy already implemented. Custom tasks are a separate
   track when no off-the-shelf eval matches.

Each eval gives us two signals at once: how good a model is at a task,
and how the inference config holds up under it.

## Hub layout

Docs hub for LLM inference on the cluster. Notes that should outlive any
one deployment go here; reusable service scripts live with the workload
(`cluster/cdk8s/ollama/`, `x/local_llm/`), shared experiment tools under
`runs/`, and run-specific evidence with its dated record.

## What's here

- <PLAN.md> — active program for mapping agent-capable models across context,
  quality, latency, conventional GPU serving, and exotic RAM/SSD/CPU-offload
  runtimes on `wyrm2`. Owns the measurement conventions and the experiment order.
- <results.md> — hand-maintained comparison table of current configurations,
  with source/trust marks on every number.
- <rugged_local_llm.md> — what runs offline on `rugged` (Ollama Vulkan, LiteRT-LM,
  NPU status), how to run it, and OpenCode tool-calling results with Gemma 4 E2B.
  Also: <runs/2026-10-13_rugged_model_speed/README.md> (model speed/memory) and
  <rugged_npu_driver.md> (NPU driver research).
- <backend_comparison.md> — feature/format/API matrix across llama.cpp, Ollama,
  vLLM, SGLang, TensorRT-LLM, and the rest. Records the earlier Ollama baseline
  and the proposed vLLM migration.
- <vllm_history.md> — distilled lessons from the prior wyrm2-host vLLM work
  (Qwen3-Coder OOM saga, AWQ + FP8 KV cache + `--max-num-seqs 32` fix). Read
  before re-attempting vLLM in cluster.
- <benchmarks.md> — legacy configuration and measurement register plus runner
  notes. Historical evidence, frozen; current numbers live in <results.md>.
- <runs/2026-07-14_glm52_colibri/README.md> — reproducible wyrm2 host run of
  GLM-5.2 through Colibri's disk-streamed MoE runtime, including the pinned
  flake, checkpoint gate, run scripts, measurements, and experiment verdict.
- <ktransformers_dsv4_notes.md> — KTransformers + DeepSeek-V4-Flash findings. Not run
  on `wyrm2`: memory-gated (needs ≥192–256 GB RAM, the workload that justifies a RAM
  upgrade).
- <qwen3_coder_vram_analysis.md> — full VRAM math, debug logs, profiler
  output. Source data for `vllm_history.md`.
- <vllm_container_plan.md> — unimplemented Home Manager systemd-user service
  proposal. The recorded vLLM runs use Kubernetes or the host launcher instead.
- <kv_cache_quantization.md> — KV cache dtype research (FP16 vs FP8 vs Q8).
- <model_download_history.md> — model search log and download status.
- <reasoning_vs_agentic_coding.md> — model selection research for the
  reasoning vs coding-agent tradeoff.
- <TODO.md> — prioritized next-steps list, ranked by information gain
  toward the goal above.

## Current state

TabbyAPI serves Qwen3.8 Flash-Next EXL3 on both RTX 5090s on `wyrm2`.
The existing LiteLLM aliases select the stock 2.68 bpw checkpoint. The
abliterated 2.50 bpw route switches the same server to a second checkpoint.
The server runs one generation at a time with a 262,144-token window and
8 GiB RAM and 16 GiB NVMe prefix caches. Ollama is scaled to zero and its
volumes are retained for rollback. Checkpoint pins, routes, and rollback
steps are in the [workload README](../../cdk8s/ollama/README.md).

- The [matched SSD run](runs/2026-11-18_dual_5090_exl3/README.md) compares
  IQ4_XS and EXL3, records the near-limit live 256K check, and describes
  the Agentplane staging tool and compaction probes.
- The [prefix-cache run](runs/2026-11-19_tabby_prefix_cache/README.md)
  measures reuse after GPU eviction, checkpoint switching, and pod replacement.
- The [November Ollama refresh](runs/2026-11-18_ollama_refresh/README.md) and
  [September SSD run](runs/2026-09-27_ollama_ssd/README.md) record the previous
  cluster baseline. The [September host run](runs/2026-09-24_qwen38_ssd/README.md)
  includes the earlier launch commands.

## Tracking

Add new lessons here as we accumulate them. When investigating a specific
incident or migration, write a focused doc and link it from this README.

## Next work

The active program prioritizes coding capability, including GPU/RAM offload and
SSD-streamed models. Keep model-card quality separate from local quantized
agent results; E1–E5 are completed historical baselines. Each run is a `runs/<run-id>/` directory (manifests/scripts + a
README with the numbers); current comparisons land in <results.md>. Persistent
individual tasks remain in <TODO.md>.

## See also

- <../../cdk8s/ollama/> — TabbyAPI deployment and retained Ollama configuration
- <../../../x/local_llm/> — wyrm2 host scripts (vLLM/Ollama/comfyui)
- <../gpu.md> — GPU/CDI runtime stack on wyrm2
