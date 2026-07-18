# Wyrm2 agent-LLM capability map and experiment plan

- **Status:** active
- **Last reviewed:** 2026-07-17

## Goal

Map the LLM configurations that `wyrm2` can run usefully for coding-agent work.
The result should answer three questions with measurements rather than model-card
claims:

1. How much context can the configuration allocate and use effectively?
2. How capable and reliable is it on coding and tool-use tasks?
3. What latency and resource cost does that capability require?

The minimum interesting total context window is 128K tokens. A usable 1M-token
configuration is the ideal outcome, but not a prerequisite for a useful result.
There is no hard latency cutoff: retain slow configurations when they occupy a
meaningful quality/context/latency Pareto frontier.

Conventional GPU-resident serving and exotic feasibility paths are peer scopes.
The latter explicitly includes Colibri and other SSD-streamed-expert runtimes,
CPU/RAM offload, KTransformers, `llama.cpp` layer or tensor splitting, mixed
weight/activation/KV dtypes, and reallocating some RAM from `atlas` to `wyrm2`.
Apply the same measurement discipline to both lanes; do not discard an approach
solely because it is unconventional.

## Posture: hobbyist scale, not reproducible science

This is one person with two RTX 5090s deciding what to serve, not a research
paper. There is no typed schema registry, no `manifest.json`/`summary.json`
interface layer, no generated reports, no formal immutability or comparability
machinery. The deliverable per experiment is:

- the launch configuration that actually ran — k8s manifests (preferred) or
  scripts, checked into the run directory;
- a run `README.md` with the numbers that matter (context reached, TTFT, decode
  tokens/s, VRAM/RAM footprint, tool-call reliability, anomalies, verdict);
- a row in <results.md>, the hand-maintained comparison table.

Two honesty rules replace the heavier apparatus:

- **Don't rewrite history in place.** Don't edit numbers in an accepted run
  README; add a new run directory and repoint the <results.md> row.
- **Every number cites its source and its trust.** Each <results.md> row links
  its run (or names the external source) and carries a trust mark.

## Quality evidence policy

Default to **published evals** for quality: model cards, official leaderboards
(LiveCodeBench, BFCL, SWE-bench Verified), and credible community results at the
same or a similar quantization. Re-running a full SWE-bench campaign on a 2-GPU
box mostly reproduces numbers other people already computed more carefully.

Run quality workloads ourselves only when:

- no external number exists for the model at a comparable quant/runtime;
- our deployment is weird enough to plausibly change results (aggressive KV
  quantization, SSD-streamed experts, approximate routing, unusual context
  extension); or
- observed behavior contradicts the external number (tool-call parse failures,
  obviously degraded output, refusals).

What we **always** measure locally, because it depends on our deployment and not
the checkpoint: context capacity, latency/throughput, resource footprint, and
tool-calling round-trip reliability through our actual served API path.

Trust marks used in <results.md>:

- `ext` — external number at similar quant/config; no reason to doubt.
- `ext?` — external number, but quant/runtime differs enough that it may not
  transfer; flagged for possible future local deepening.
- `local` — measured here; run link required.
- `local~` — quick local probe (e.g. needle checks standing in for a full
  long-context eval); indicative, not definitive.

## Where workloads run

**Kubernetes-first.** `wyrm2` is a cluster node with both GPUs exposed
(`runtimeClassName: nvidia`, `nvidia.com/gpu: 2`; the working pattern is
<../../k8s/ollama/app/deployment.yaml>). Serve each candidate as an ad-hoc
Deployment (or bare Pod) plus a bench Job, applied straight with `kubectl apply
-f runs/<run-id>/` — **not** wired into Flux. Only a configuration we decide to
keep gets promoted into a Flux-managed directory under `cluster/k8s/` and
registered in LiteLLM.

Host-level runs are the exception, used when the runtime needs host control the
cluster can't easily give it (Colibri's SSD-streaming path, KTransformers
experiments, systemd memory-capped RAM trials). Those keep their scripts in the
run directory, as the existing GLM-5.2 Colibri run already does.

Model weights reuse the existing model PVC/hostPath pattern from the Ollama
deployment; a download Job lives alongside the serving manifest in the run dir.

## Repository structure

```text
cluster/docs/inference/
  README.md                 dashboard and navigation
  PLAN.md                   this program
  results.md                hand-maintained comparison table (the numbers)
  TODO.md                   prioritized next experiments
  benchmarks.md             historical evidence (frozen)
  runs/<run-id>/
    README.md               numbers, anomalies, verdict
    *.yaml / *.sh           the manifests / scripts that ran
  archive/                  superseded plans and historical research
```

Superseded docs (`vllm_container_plan.md`, `model_download_history.md`, dated
model-selection research) move to `archive/` once their durable conclusions are
reflected in the current docs. Large eval logs stay out of git — link them from
the run README or just summarize the headline number.

## Measurement conventions

Kept deliberately small, so two runs a month apart stay roughly comparable.

- **Context.** For a target total window `C`, submit about `C − 4096` input
  tokens, reserving 4,096 for output. Record three values:
  - **advertised** — model/runtime claim (not a measurement);
  - **allocated** — largest `C` that loads and completes one request without
    OOM/overflow/timeout;
  - **effective** — largest `C` passing a quick needle probe (a few needles at
    several insertion depths, exact match). The needle probe is `local~`: it
    catches gross long-context breakage, not subtle degradation — lean on
    external RULER-style results for the latter where they exist.
- **Latency.** Single concurrent request, temperature 0, 256-token generation.
  Report TTFT and decode tokens/s at 8K, 32K, 128K input, and at each larger
  allocated context. A handful of repetitions is enough; note warm vs cold. For
  offload/SSD runtimes, report cold-start and warmed-steady-state separately —
  the steady-state number never replaces the cold one.
- **Resources.** Peak per-GPU VRAM (`nvidia-smi`); for offload runs also RSS,
  page cache, and SSD read throughput while decoding.
- **Tool calling.** Through the served OpenAI-compatible API: one single-call,
  one parallel-call, and one multi-turn round trip with fixed schemas. Record
  parse failures and whether reasoning/tool state survives the round trip. This
  is the cheapest local check that predicts real agent usability.
- **Failures.** Say what actually failed — startup OOM, allocation OOM, parser,
  timeout, garbage output — not just "failed".

## Configuration space

Avoid an exhaustive Cartesian product. Screen a credible configuration for each
architecture/runtime family, then vary one axis at a time for the models that
show useful capability. Never compare two differently quantized checkpoints and
attribute the difference to the runtime alone.

### Runtime families

| Family               | Initial role                                                                                                                          |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| vLLM                 | First GPU-resident runtime for supported FP8, MXFP4, and AWQ models; tensor parallel across both GPUs.                                |
| SGLang               | Same-model comparison when prefix caching, tool parsing, or vLLM latency is limiting.                                                 |
| `llama.cpp` / Ollama | GGUF, CPU/GPU layer splitting, memory mapping, host-RAM-heavy configs. Use `llama.cpp` directly when Ollama hides a required control. |
| KTransformers        | Large MoE expert offload and heterogeneous CPU/GPU placement.                                                                         |
| Colibri              | SSD-streamed experts and deliberately storage-bound models such as GLM-5.2.                                                           |

### Model candidates

Run the resident and exotic/offload lanes concurrently. "Initial order" is order
within a lane, not an instruction to postpone the exotic lane.

#### GPU-resident or near-resident lane

| Initial order | Candidate                                                                                              | First configuration                          | Question                                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 1             | [NVIDIA Nemotron 3 Nano 30B-A3B FP8](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8) | vLLM, FP8 KV                                 | Can its hybrid architecture deliver an effective 1M context within current VRAM?                   |
| 2             | [Qwen3.5-35B-A3B FP8](https://huggingface.co/Qwen/Qwen3.5-35B-A3B)                                     | vLLM, FP8 KV                                 | How much of its native 262K and extended ~1M context is usable on two 5090s?                       |
| 3             | [Qwen3-Coder-Next GGUF](https://huggingface.co/Qwen/Qwen3-Coder-Next-GGUF)                             | `llama.cpp`, Q4_K_M                          | Does coding specialization outweigh the latency and quantization cost of its larger total weights? |
| 4             | [Devstral Small 2 24B](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512)            | vLLM, FP8                                    | Establish a dense coding-agent quality and latency baseline around 256K.                           |
| Baseline      | Qwen3-Coder-30B-A3B AWQ                                                                                | Known vLLM TP2 profile, FP8 KV               | Reproduce the known 262K configuration, now in k8s.                                                |
| Baseline      | gpt-oss-20B                                                                                            | vLLM native MXFP4 vs the existing Ollama one | Establish the fast 128K-class floor and isolate runtime effects.                                   |
| Secondary     | [GLM-4.7-Flash](https://huggingface.co/zai-org/GLM-4.7-Flash)                                          | Supported 4-bit runtime                      | Test another coding-oriented small-active-parameter MoE around 200K.                               |

Defer dense Qwen3.5-27B and models below 128K unless an observed result makes
them answer a specific question this set does not.

#### Exotic/offload lane

| Initial order | Candidate or mechanism                                                                  | First configuration                                                                        | Question                                                                                                                                                                                                     |
| ------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1             | GLM-5.2                                                                                 | Existing Colibri INT4-expert/INT8-MTP run, then lower activation/KV precision if supported | How do context, page cache, SSD throughput, and drafting interact in a disk-streamed expert model?                                                                                                           |
| 2 (E10)       | [MiniMax-M3](https://huggingface.co/MiniMaxAI/MiniMax-M3) (428B / ~23B active, MSA, 1M) | `llama.cpp` mainline IQ2 GGUF, `--cpu-moe` offload (E9 wiring)                             | Does its +1.5 SWE over DSV4-Flash (80.5 vs 79.0) survive ~2× active params (23B vs 13B → slower) and a larger footprint — i.e. does it _extend_ the offload coding frontier or just shift DSV4's point left? |
| 3             | [Devstral 2 123B](https://huggingface.co/mistralai/Devstral-2-123B-Instruct-2512)       | Q4 GGUF or another supported CPU/GPU split                                                 | What is the quality ceiling for a dense model spanning both VRAM and host RAM?                                                                                                                               |
| 4             | Best large GGUF candidate from the resident screen                                      | `llama.cpp` tensor/layer split with `mmap`                                                 | Where is the practical frontier between resident KV, offloaded weights, and page cache?                                                                                                                      |
| Mechanism     | More `wyrm2` RAM                                                                        | 104 GiB controlled trial after in-allocation sensitivity tests                             | Does another 8 GiB cross a fit/cache boundary, or merely move an already poor result slightly?                                                                                                               |

Add newly released runtimes and architectures when they plausibly change the
frontier. Being unsupported by vLLM is not a reason to exclude a model; it is a
reason to test the runtime that exposes its intended memory hierarchy.

### Refinement axes

For the best configurations in either lane, vary one axis at a time, in this
order, re-running the context probe and the 8K/128K latency workload:

1. Weight precision or quantization supported by the same runtime.
2. KV dtype at fixed weights and context.
3. Runtime at fixed checkpoint and prompt protocol.
4. Expert/layer residency, CPU/GPU split, memory mapping, and storage cache.
5. Context allocation and concurrency.

## First five experiments

Each experiment is one run directory: serving manifest(s), a bench Job, and a
README following the conventions above (context ladder; latency at 8K/32K/128K;
tool-call smoke; VRAM/RAM footprint; verdict). Quality comes from external evals
unless a trust mark says otherwise.

### E1 — k8s vLLM baseline: Qwen3-Coder-30B-A3B AWQ, TP2, FP8 KV, 262K

Port the known-good host configuration (`--tensor-parallel-size 2
--kv-cache-dtype fp8 --max-model-len 262144 --gpu-memory-utilization 0.90
--max-num-seqs 32`; see <vllm_history.md>) into a k8s Deployment on `wyrm2`.

- **Measure:** does the k8s + CDI + vLLM path work at all; allocated context at
  128K and 256K; TTFT and decode tokens/s at 8K/32K/128K; tool-call smoke;
  per-GPU peak VRAM.
- **Quality:** external Qwen3-Coder numbers (`ext`).
- **Why first:** validates the whole harness against a configuration whose host
  behavior we already know, and produces the reference row in <results.md>.

### E2 — runtime isolation on the incumbent: gpt-oss-20b, vLLM MXFP4 vs Ollama

Same checkpoint family we already serve, two runtimes: vLLM with native
Blackwell MXFP4 (single GPU, then TP2) versus the live Ollama deployment (which
dequantizes to bf16).

- **Measure:** decode tokens/s and TTFT at 8K and 128K; tool-call smoke;
  `reasoning_effort` behavior on each runtime.
- **Quality:** HumanEval already saturated here (`local`, prior run); no new
  quality run — this experiment is about the runtime, not the model.
- **Why:** decides whether the cluster's fast 128K-class default endpoint should
  move off Ollama, and calibrates how much runtime choice alone is worth.

### E3 — the 1M attempt: Nemotron 3 Nano 30B-A3B FP8, vLLM TP2, FP8 KV

The hybrid-architecture candidate whose KV footprint should be small enough for
a serious long-context attempt.

- **Measure:** context ladder 128K → 256K → 512K → 1M — largest allocated
  window, needle probe at each rung (`local~`), TTFT/decode at 128K and at the
  largest allocated window, VRAM/KV budget at each rung.
- **Quality:** NVIDIA's published numbers (`ext`).
- **Why:** the headline question of the whole program — what is the largest
  _effective_ window this hardware can host at all.

### E4 — current-gen generalist MoE: Qwen3.5-35B-A3B FP8, vLLM TP2, FP8 KV

The strongest recent generalist that plausibly fits resident.

- **Measure:** allocated context at native 262K (then its ~1M extended mode if
  the runtime supports it); latency curve; tool-call smoke.
- **Quality:** published Qwen3.5 evals at FP8 (`ext`), compared against E1's
  Qwen3-Coder externals.
- **Why:** the "is there a free upgrade over the 2025 coding baseline"
  experiment.

### E5 — dense coding-agent baseline: Devstral Small 2 24B FP8, vLLM TP2, 256K

The dense counterpoint to the MoEs — expected slower decode but strong published
agentic numbers.

- **Measure:** allocated context at 256K; latency at 8K/32K/128K; tool-call
  smoke — Mistral function calling through vLLM's parser is exactly the kind of
  thing that breaks in deployment-specific ways, so a misbehavior here is a
  `local` finding external evals can't give us.
- **Quality:** published Devstral SWE-bench/agentic numbers (`ext`).
- **Why:** quantify the MoE-vs-dense latency gap at equal VRAM and decide
  whether dense quality is worth it.

The exotic lane continues in parallel (GLM-5.2 Colibri follow-ups per that run's
TODO list); it is not gated on E1–E5.

### E10 — MiniMax M3: can it extend the offload coding frontier over DSV4-Flash?

The only recently-released open weight that is a genuine Pareto _candidate_ above
DeepSeek-V4-Flash (E9) on coding: **MiniMax M3**, 428B total / ~23B active MoE, MSA
sparse attention, native 1M context, open-weight. External evals: SWE-bench Verified
80.5, GPQA Diamond 92.9 (HLE numbers are protocol-split — do not cite until pinned).

- **Runtime:** reuse the E9 wiring verbatim — mainline `ggml-org/llama.cpp`, an IQ2 GGUF
  (~107 GB at 2 bpw), `--cpu-moe` expert offload, Vulkan (`-ngl 999 -c 4096`), NVIDIA
  ICD. First confirm an IQ2/IQ1 GGUF exists (unsloth) that targets mainline; the MSA
  attention must be merged in llama.cpp (verify, as with DSV4's HCA tensors).
- **Measure:** decode tok/s (CPU floor + Vulkan) — the key number, since ~23B active vs
  DSV4's 13B predicts roughly half the decode rate; largest allocated context; coherence
  - tool-call smoke; peak VRAM/RAM and how much of the 107 GB page-caches in 96 GB.
- **Question:** does +1.5 SWE over DSV4-Flash (80.5 vs 79.0) survive being ~2× slower and
  ~16 GB larger — i.e. does M3 add a new point _above_ DSV4 on the speed×SWE frontier, or
  is it strictly dominated (slower for negligible quality)? A dominated result is still a
  finding: it says DSV4-Flash is the offload coding sweet spot and bigger MoEs don't help
  at this memory budget.
- **Explicitly NOT queued: Inkling** (Thinking Machines, 975B / 41B active). At ~244 GB
  IQ2 it exceeds 96 GB RAM + practical SSD streaming, and 41B active would crawl at
  GLM-5.2 tier (~0.1–0.3 tok/s). Too big to run usefully on `wyrm2`; revisit only with a
  much smaller quant or more RAM.

## Later work

- **RAM sensitivity and `atlas` safety.** Test offload configurations inside the
  current 96 GiB first, via systemd memory caps (80/88/96 GiB). A 104 GiB
  host-allocation trial is a separate declarative Terraform change, made only if
  another 8 GiB would cross a demonstrated fit boundary or predict ≥20%
  wall-time/storage-traffic reduction; watch `atlas` memory pressure and revert
  unless a kept configuration depends on it. Do not use the former 112 GiB
  allocation — the existing Terraform record says it left ~8 GiB for host + ZFS
  and caused stalls.
- **LiteLLM integration.** A kept configuration gets promoted to a Flux-managed
  deployment, added to cluster LiteLLM, and re-smoked through the gateway
  (API/tool round trip, a 128K request, a Haku-style tool job). Record gateway
  overhead separately from backend latency.
- **Deepening `ext?` numbers.** Any external number that starts driving a real
  decision (e.g. picking the default coding model) becomes a candidate for a
  local eval run; <TODO.md> tracks these individually.
- **Intelligence-ceiling axis: bigger/smarter models at the edge of runnable.**
  The E1–E5 set is deliberately mid-size (fast, resident). A worthwhile separate
  axis is "how much more capability can we get if we accept it's slow / barely
  fits" — e.g. **gpt-oss-120b** (MXFP4 ≈ 63 GB, right at the 64 GB aggregate-VRAM
  edge; likely needs a little CPU/RAM offload or aggressive KV/context limits),
  and similarly large MoEs. This overlaps the exotic/offload lane's premise —
  the GLM-5.2-on-Colibri appeal is "full frontier-class quality, just slow" —
  but framed as its own knob: trade latency/fit for raw model intelligence and
  measure the capability-per-slowdown. Worth a dedicated run once E1–E5 map the
  fast tier.
