# Inference backends

## Goal

Two RTX 5090s on `wyrm2`. The point of this directory is to figure out
**how to make them do useful work for me** — and to keep doing so as
models, configs, and tasks evolve. Three things to decide:

1. **Which models** — quality on jobs that matter (AI-powered coding;
   reverse engineering, see <../../../skills/reverse_engineer/>; FreeCAD,
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
one deployment go here; runnable scripts live with the workload
(`cluster/k8s/ollama/`, `x/local_llm/`).

## What's here

- <backend_comparison.md> — feature/format/API matrix across llama.cpp, Ollama,
  vLLM, SGLang, TensorRT-LLM, and the rest. Includes current cluster state
  and migration path. Decision document for picking what to run on wyrm2.
- <vllm_history.md> — distilled lessons from the prior wyrm2-host vLLM work
  (Qwen3-Coder OOM saga, AWQ + FP8 KV cache + `--max-num-seqs 32` fix). Read
  before re-attempting vLLM in cluster.
- <benchmarks.md> — known measurements per (backend, model, flags)
  configuration, caveats that bit us, and an off-the-shelf eval runner
  cheat sheet (simple-evals, lm-eval-harness, evalplus, BFCL, …). Update
  rows when you bring up or rerun a config.
- <qwen3_coder_vram_analysis.md> — full VRAM math, debug logs, profiler
  output. Source data for `vllm_history.md`.
- <vllm_container_plan.md> — home-manager systemd-user service plan that
  ran vLLM on wyrm2.
- <kv_cache_quantization.md> — KV cache dtype research (FP16 vs FP8 vs Q8).
- <model_download_history.md> — model search log and download status.
- <reasoning_vs_agentic_coding.md> — model selection research for the
  reasoning vs coding-agent tradeoff.
- <TODO.md> — prioritized next-steps list, ranked by information gain
  toward the goal above.

## Current state (2026-04-28)

- **Cluster inference**: Ollama Deployment on wyrm2, GGUF only, no tensor
  parallel. See <../../k8s/ollama/app/deployment.yaml>.
- **Host experiments**: `x/local_llm/` on wyrm2 (systemd-user + Docker).
  Has working vLLM AWQ scripts but never moved to k8s.

Full table in <backend_comparison.md#current-state-2026-04-28>.

## Tracking

Add new lessons here as we accumulate them. When investigating a specific
incident or migration, write a focused doc and link it from this README.

## TODO

- **Consolidate per-run `bench.py` copies into one shared script.** Each
  `runs/<date>_<name>/` currently carries a `bench.py` snapshot to
  preserve the run-as-commit invariant. As the bench stabilizes, move it
  to e.g. `cluster/docs/inference/bench/bench.py` and have run dirs only
  store a manifest, env, and output. The snapshot invariant can then be
  preserved by recording the bench commit hash in the run README.

## See also

- <../../k8s/ollama/> — current cluster Ollama deployment
- <../../../x/local_llm/> — wyrm2 host scripts (vLLM/Ollama/comfyui)
- <../../README.md#gpu-nvidia> — GPU/CDI runtime stack on wyrm2
