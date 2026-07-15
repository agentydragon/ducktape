# Wyrm2 agent-LLM capability map and experiment plan

- **Status:** active
- **Last reviewed:** 2026-07-14

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

## Deliverables and repository structure

Refactor this hub toward the following ownership model as the harness is built:

```text
cluster/docs/inference/
  README.md                 current dashboard and navigation
  PLAN.md                   this active program
  candidates.yaml           candidate and configuration registry
  methodology.md            frozen protocol and metric definitions
  results.md                generated comparisons and Pareto views
  TODO.md                   remaining experiments
  runs/<run-id>/
    README.md               conclusion, anomalies, and follow-ups
    manifest.json           reproducible inputs
    summary.json            normalized measurements
  archive/                  superseded plans and historical research

x/local_llm/bench/
  README.md
  BUILD.bazel
  schemas.py
  configs/                  runtime launch profiles
  runner/                   measurement and eval orchestration
  report/                   validation and report generation
```

- Keep unit tests adjacent to their modules per repository convention; use a
  `tests/` directory only for genuinely cross-package integration tests.
- Keep reusable launch, measurement, and reporting code under
  `x/local_llm/bench/`; documentation must not own executable infrastructure.
- Keep genuinely one-off environments with their immutable run. The Colibri
  run's pinned flake, checkpoint checks, patches, and scripts remain under
  `runs/2026-07-14_glm52_colibri/`.
- Move completed plans, raw investigations, and superseded model searches to
  `archive/`, preserving links from current conclusions where useful.
- Generate `results.md` from run summaries instead of maintaining competing
  benchmark tables by hand.
- Do not create per-run `epistemic_state.md` files. Candidate uncertainty
  belongs in `candidates.yaml`; observed anomalies and conclusions belong in
  the run record.
- Store large Inspect logs, evaluator work directories, traces, and telemetry
  in a private `llm-evals` S3 bucket. Commit the artifact URI, SHA-256, byte
  size, and normalized summary rather than large raw outputs.

The initial documentation cleanup should:

- Retain `README.md` as a compact current dashboard and index.
- Use <methodology.md> as the normative protocol and metric authority; retain
  `benchmarks.md` only as historical evidence until generated results replace it.
- Replace its hand-maintained current-results sections with generated
  `results.md`.
- Merge durable runtime facts from `backend_comparison.md`, `vllm_history.md`,
  and `kv_cache_quantization.md` into current runtime guidance without copying
  volatile model lists.
- Archive `vllm_container_plan.md`, `model_download_history.md`, the raw Qwen
  VRAM investigation, and dated model-selection research once their durable
  conclusions have an active home.
- Leave `props/docs/local_llm_evaluation/benchmarks.md` component-owned and
  historical; link it from the archive instead of duplicating it here.

## Reproducibility interfaces

<methodology.md> is normative for identity, provenance, comparability, metrics,
and acceptance. Implement three validated interfaces:

- `candidates.yaml` records the complete model, runtime, precision, placement,
  cache, context, parser, sampling, and API configuration without inferring
  unknown dtypes.
- `manifest.json` freezes configuration, host, evaluator, selected samples,
  token budgets, rendered launch configuration, and artifact destinations before
  measurement begins.
- `summary.json` contains only normalized, decision-relevant capacity, context,
  latency, resource, quality, confidence, and reliability results.

Accepted run records are immutable. Corrections and alternate scoring create a
new derived run. Checked-in typed schemas validate every interface before report
generation.

## Configuration space

Avoid an exhaustive Cartesian product. Screen a credible configuration for each
architecture/runtime family, then vary one axis at a time for the models that
show useful capability.

### Runtime families

| Family                  | Initial role                                                                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| vLLM                    | First GPU-resident runtime for supported FP8, MXFP4, and AWQ models; tensor parallel across both GPUs.                                           |
| SGLang                  | Same-model comparison when prefix caching, tool parsing, or vLLM latency is limiting.                                                            |
| `llama.cpp` / Ollama    | GGUF, CPU/GPU layer splitting, memory mapping, and host-RAM-heavy configurations. Use `llama.cpp` directly when Ollama hides a required control. |
| KTransformers           | Large MoE expert offload and heterogeneous CPU/GPU placement.                                                                                    |
| Colibri                 | SSD-streamed experts and deliberately storage-bound models such as GLM-5.2.                                                                      |
| Transformers/Accelerate | Compatibility probe or reference implementation, not the presumed serving winner.                                                                |

### Model candidates

Run the resident and exotic/offload lanes concurrently once the common harness
works. "Initial order" is order within a lane, not an instruction to postpone
the exotic lane until resident serving is complete.

#### GPU-resident or near-resident lane

| Initial order | Candidate                                                                                              | First configuration                          | Question                                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 1             | [NVIDIA Nemotron 3 Nano 30B-A3B FP8](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8) | vLLM, FP8 KV                                 | Can its hybrid architecture deliver an effective 1M context within current VRAM?                   |
| 2             | [Qwen3.5-35B-A3B FP8](https://huggingface.co/Qwen/Qwen3.5-35B-A3B)                                     | vLLM, FP8 KV                                 | How much of its native 262K and extended roughly 1M context is usable on two 5090s?                |
| 3             | [Qwen3-Coder-Next GGUF](https://huggingface.co/Qwen/Qwen3-Coder-Next-GGUF)                             | `llama.cpp`, Q4_K_M                          | Does coding specialization outweigh the latency and quantization cost of its larger total weights? |
| 4             | [Devstral Small 2 24B](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512)            | vLLM, FP8                                    | Establish a dense coding-agent quality and latency baseline around 256K.                           |
| Baseline      | Qwen3-Coder-30B-A3B AWQ                                                                                | Existing vLLM TP2 profile, FP8 KV            | Reproduce the known 262K configuration under the common protocol.                                  |
| Baseline      | gpt-oss-20B                                                                                            | vLLM native MXFP4, then existing Ollama form | Establish the fast 128K-class floor and isolate runtime effects.                                   |
| Secondary     | [GLM-4.7-Flash](https://huggingface.co/zai-org/GLM-4.7-Flash)                                          | Supported 4-bit runtime                      | Test another coding-oriented small-active-parameter MoE around 200K.                               |

Defer dense Qwen3.5-27B and models below 128K unless an observed result makes
them answer a specific question that this set does not.

#### Exotic/offload lane

| Initial order | Candidate or mechanism                                                            | First configuration                                                                        | Question                                                                                                    |
| ------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| 1             | GLM-5.2                                                                           | Existing Colibri INT4-expert/INT8-MTP run, then lower activation/KV precision if supported | How do context, page cache, SSD throughput, and drafting interact in a disk-streamed expert model?          |
| 2             | [MiniMax-M2.5](https://huggingface.co/MiniMaxAI/MiniMax-M2.5)                     | Quantized KTransformers expert offload                                                     | Can a much larger coding MoE produce enough quality at its 196,608-token window to justify RAM/SSD traffic? |
| 3             | [Devstral 2 123B](https://huggingface.co/mistralai/Devstral-2-123B-Instruct-2512) | Q4 GGUF or another supported CPU/GPU split                                                 | What is the quality ceiling for a dense model spanning both VRAM and host RAM?                              |
| 4             | Best large GGUF candidate from the resident screen                                | `llama.cpp` tensor/layer split with `mmap`                                                 | Where is the practical frontier between resident KV, offloaded weights, and page cache?                     |
| Mechanism     | SSD-streamed or selectively resident experts                                      | Colibri or another runtime with explicit expert residency controls                         | Can storage-aware expert caching create a useful steady state even when the full checkpoint exceeds RAM?    |
| Mechanism     | More `wyrm2` RAM                                                                  | 104 GiB controlled trial after the 96 GiB sensitivity test                                 | Does another 8 GiB cross a fit/cache boundary, or merely move an already poor result slightly?              |

Add newly released runtimes and architectures when they plausibly change the
frontier. Being unsupported by vLLM is not a reason to exclude them; it is a
reason to test the runtime that exposes their intended memory hierarchy.

### Refinement axes

For the best configurations in either lane, compare in this order:

1. Weight precision or quantization supported by the same runtime.
2. KV dtype at fixed weights and context.
3. Compute/activation dtype where it is a real supported control.
4. Runtime at fixed checkpoint and prompt protocol.
5. Expert/layer residency, CPU/GPU split, memory mapping, and storage cache.
6. Context allocation and concurrency.

Do not compare two differently quantized checkpoints and attribute the result to
the runtime alone.

## Experiment sequence

### Phase 0: Build the common harness and normalize existing evidence

- Implement schema validation, OpenAI-compatible streaming measurement, host
  telemetry capture, report generation, resumable runs, and artifact upload.
- Pin evaluator environments and runtime launch profiles through the repo's
  Bazel/Nix workflows or immutable image digests.
- Import existing measurements into normalized summaries without representing
  them as new or methodologically comparable runs.
- Re-run Qwen3-Coder-30B-A3B and gpt-oss-20B to validate the harness and expose
  drift from the historical results.

Exit criterion: one command can launch or identify a service, run a smoke
workload, capture a valid manifest and summary, upload raw artifacts, and
regenerate `results.md`.

### Phase 1: Admission and API behavior

For every configuration in both lanes:

- Verify model load, tokenizer/chat template, streaming termination, finish
  reasons, reasoning fields, tool-call parsing, and context-limit errors.
- Exercise single, parallel, and multi-turn tool calls with fixed schemas.
- Confirm that reasoning and tool state survive a complete tool round-trip.
- Classify failures as checkpoint, runtime support, startup OOM, allocation OOM,
  protocol, parser, timeout, or incorrect result rather than recording only
  "failed."

Exit criterion: the service can complete a short deterministic request and a
multi-turn tool request, or has a reproducible terminal failure classification.

### Phase 2: Allocated and effective context

Attempt total windows of 128K, 256K, 512K, and 1,000K where claimed or technically
possible. For target window `C`, submit `C - 4096` input tokens with a 4,096-token
output reserve; record both requested and tokenizer-observed lengths.

At each allocated length, run a fixed RULER-style subset covering:

- Single-needle retrieval.
- Multiple keys and multiple needles.
- Variable tracking or aggregation that cannot pass through one lexical match.
- Eight insertion depths and three seeds per task/length.

Record three distinct values:

- **Advertised context:** checkpoint or runtime claim.
- **Allocated context:** largest request accepted and completed without OOM.
- **Effective context:** largest tested length whose mean exact score is at least
  90% and whose worst depth band is at least 80%.

`capacity-qualified` means a completed 128K request with the output reserve.
`agent-viable` means effective context of at least 128K plus a 30-minute
growing-history/tool-call soak without an OOM or protocol failure. Keep results
that miss these labels in the map when they illuminate a frontier.

For Colibri and other offload configurations, repeat context probes after both a
cold start and a warmed expert/page cache. Record time to reach steady state.

### Phase 3: Latency and resource curves

Measure at 8K, 32K, 128K, and every larger qualified context:

- One concurrent request.
- Fixed 256-token generation.
- Temperature zero and a fixed reasoning mode.
- 20 warm repetitions at 8K and 128K, five at larger contexts, and three cold
  starts.
- Separate cold-prefix, warm-prefix, cold-model/cache, and steady-state results.

Capture:

- Time to first token (TTFT), end-to-end latency, and inter-token latency at
  p50/p95.
- Decode tokens/s and prefill tokens/s where server telemetry can distinguish
  them.
- Model load and first-request time.
- Per-GPU peak VRAM, utilization, clocks, power, and temperature.
- Process RSS, anonymous memory, page cache, CPU utilization, major faults,
  swap, storage read bytes/latency/queue depth, and PCIe traffic.
- Prefix-cache hit state and cache capacity.

For disk/expert offload, also capture throughput per generated token, expert
cache hit/miss data when exposed, and the warmup curve across consecutive
requests. The steady-state number never replaces the cold number; report both.

### Phase 4: Coding and tool-quality funnel

Use the evaluator's standard/default harness, pinned unchanged across models.
The screening campaign is deliberately independent of Claude Code, OpenCode,
and Haku so scaffold differences do not decide the model ranking.

Initial screen:

- LiveCodeBench: fixed, date-stratified 100-problem subset.
- BFCL: fixed 200-case subset spanning simple, multiple, parallel, and
  multi-turn calls.
- SWE-bench Verified through the canonical pinned Inspect agent: fixed,
  seeded 20-task pilot.

Approximately three conventional finalists and up to two exotic finalists:

- Full pinned LiveCodeBench release/window.
- Full selected BFCL categories.
- Seeded 100-task SWE-bench Verified run.
- Three repeats of the 20-task SWE-bench pilot to estimate run-to-run
  reliability.

Deployment candidates:

- Full 500-task SWE-bench Verified run if the 100-task result remains useful.
- LiteLLM compatibility through both OpenAI Chat Completions and Anthropic
  Messages shapes.
- Twenty Haku-style tool jobs and a 24-hour idle/load soak. This validates the
  intended integration but is not the primary quality score.

Apply the same quality funnel to an exotic configuration whenever it can
finish the workload within a declared time budget. If it cannot, run the fixed
pilot and report the projected full-run time rather than silently excluding it.

### Phase 5: Precision, runtime, and residency refinement

Hold the model, prompt, evaluator, and sampling fixed while changing one axis.
Re-run context qualification, the 128K performance workload, the BFCL subset,
and the repeated SWE-bench pilot.

Treat a lower-precision or more aggressively offloaded configuration as
quality-non-inferior only when its confidence interval overlaps the reference
and it produces a material context, latency, or capacity improvement. Retain
quality losses in the report rather than folding them into a single score.

### Phase 6: `wyrm2` RAM sensitivity and `atlas` safety

The current baseline is 96 GiB dedicated to `wyrm2`; ballooning is disabled for
GPU passthrough. First test offload configurations inside that allocation using
systemd scopes capped at 80 GiB, 88 GiB, and the current unrestricted 96 GiB.
Measure OOM margin, resident versus cached bytes, major faults, SSD reads, cold
latency, and steady-state throughput.

Consider more RAM only if another 8 GiB would:

- Cross a demonstrated model/context fit boundary, or
- Predict at least a 20% reduction in wall time or storage traffic.

The first host-allocation trial is 104 GiB. Make it as a separate declarative
Terraform change, because it requires a `wyrm2` restart, and record `atlas`
memory availability, ZFS behavior, and memory-pressure metrics before and
during the run. Abort and revert on memory PSI, ZFS stalls, or an unsafe
available-memory floor.

Do not use the former 112 GiB allocation under the current `atlas` workload:
the existing Terraform record says that it left only about 8 GiB for the host
and ZFS and caused stalls. Reaching beyond 104 GiB requires first moving or
resizing the competing `atlas` workload; it is an infrastructure experiment,
not an inference flag.

Revert to 96 GiB after the experiment unless a selected configuration
demonstrably depends on 104 GiB. A null result is useful: it confirms that
model/runtime/precision changes dominate small host-RAM reallocations.

### Phase 7: Cluster and LiteLLM integration

Host experiments precede Kubernetes deployment so storage, CDI, scheduling,
and gateway behavior do not obscure inference feasibility.

For a selected configuration:

- Reproduce the host launch in Kubernetes with pinned images/configuration and
  an SSD-backed storage class when the runtime depends on SSD behavior.
- Preserve host-measured memory and context controls explicitly in the
  workload manifest.
- Add the model to cluster LiteLLM only after direct-backend protocol and soak
  checks pass.
- Re-run the API/tool smoke, 128K capacity check, warm/cold latency probe, and
  Haku-style soak through LiteLLM.
- Record gateway overhead separately from backend latency.

For exotic runtimes without an OpenAI-compatible server, a thin adapter is in
scope after the runtime proves useful directly. Do not make adapter work a
prerequisite for answering whether the model can run.

## Metrics and computation

<methodology.md> owns the normative formulas, cache conditions, uncertainty,
reliability taxonomy, and Pareto eligibility. Generate views for:

- best effective-context ceiling;
- best coding quality at or above 128K;
- best 128K latency;
- best 1M attempt;
- best quality reachable through RAM/SSD/CPU offload;
- quality versus p95 agent time to solution, colored by effective context and
  shaped by runtime/quantization family.

Do not calculate a weighted best-model score or compare results that the
methodology classifies as contextual or historical.

## Verification

Harness tests cover:

- Schema rejection and stable configuration/run IDs.
- Exact token-length construction and output reservation.
- Streaming event timing, finish reasons, and reasoning/tool event handling.
- Metric formulas and Wilson intervals.
- Artifact hashing/upload metadata and resumable/idempotent runs.
- Deterministic sample selection and report generation.
- Cold/warm cache labels that cannot be omitted or conflated.

Use a fake OpenAI-compatible server for streaming and failure-path tests. Give
every external evaluator a one-sample end-to-end smoke before a campaign.
Re-running a manifest must select the same configuration and dataset IDs.

Use Bazel for checked-in harness tests. Before handing off an implementation
change, run focused targets followed by `bbr build //...` and `bbr test //...`;
document unrelated repository-wide blockers rather than weakening a gate.

## Working assumptions

- `wyrm2` is a Proxmox VM with two passed-through RTX 5090 GPUs, 64 GB aggregate
  VRAM, no GPU P2P, 96 GiB host RAM, and an existing SSD path used by Colibri.
- Context means the total model window with an explicit output reserve, not the
  largest accepted input alone.
- Standardized coding/tool evals define the initial quality comparison. Haku is
  a downstream compatibility and soak workload until it has its own stable
  scored eval.
- Single-request agent use is the first performance target. Add concurrency
  measurements only when a concrete cluster workload needs them.
- Historical run records remain immutable and clearly labelled when their
  protocol differs from the new methodology.
- The campaign is a funnel within each runtime lane, not a policy that exotic
  approaches must wait for or imitate GPU-resident serving.
