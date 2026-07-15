# Inference benchmark methodology

- **Protocol:** `inference-bench/v1`
- **Status:** active
- **Last reviewed:** 2026-07-14

## Scope and ownership

This document is the normative measurement and comparison protocol for the
`wyrm2` inference campaign. <PLAN.md> defines the program goal, candidate funnel,
experiment order, and promotion gates. Candidate membership and current status
belong in `candidates.yaml`; accepted observations belong in run manifests and
summaries; `results.md` is generated from those records.

The GPU-resident and exotic/offload lanes use the same protocol. A runtime does
not need to expose an OpenAI-compatible server to qualify: the harness may use a
pinned adapter, and the manifest records the native and evaluated API shapes.

Historical measurements in <benchmarks.md> remain evidence, but they are not
protocol-comparable unless an import review establishes that they captured every
required condition. The generated report must default an incomplete import to
`historical` rather than infer missing configuration or measurement fields.

## Protocol and schema versions

Every run records:

- `protocol_version`, initially `inference-bench/v1`;
- `manifest_schema_version`;
- `summary_schema_version`.

Change `protocol_version` when a change can alter a measured value, eligibility,
or comparability: sample construction, output reservation, cache conditions,
timing boundaries, resource definitions, evaluator/scorer behavior, failure
taxonomy, aggregation, or report admission. A schema-only migration may retain
the protocol version when normalized semantics do not change.

A run pins the repository commit and protocol version it used. Later methodology
changes never relabel an accepted run in place.

## Candidate, configuration, and run identity

A candidate ID has the stable form:

```text
<model>-<weight-format>-<runtime>-<kv-dtype>-<max-context>
```

Use normalized, filesystem-safe components. The registry records the display
names and upstream identifiers; reports do not recover meaning by parsing the
ID.

The candidate ID is not the complete configuration. Each manifest records the
full behaviorally relevant configuration and its SHA-256 digest over canonical
JSON, including:

- model repository, revision, selected files, hashes, license, parameter counts,
  architecture, and advertised/native context;
- runtime revision or image digest and checked-in launch profile;
- weight, compute, activation, accumulator, and KV-cache dtypes where exposed;
- tensor parallelism, GPU/CPU split, expert/layer placement, memory mapping,
  offload, storage, maximum context, maximum sequences, and prefix-cache settings;
- chat template, tool and reasoning parsers, reasoning mode, sampling settings,
  and API shape.

Never infer activation or accumulator dtype from weight quantization. An unknown
value remains explicitly unknown.

A run ID identifies one execution of one immutable configuration against pinned
workloads and environment. A correction, rerun, alternate scorer, or regrade
creates a new run that references its parent and the original artifact.

## Comparability classes

Every normalized result has one class:

- **`controlled`:** Same protocol, hardware class, evaluator and scorer,
  dataset revision and sample IDs, prompt/scaffold, API shape, sampling,
  reasoning mode, concurrency, and cache condition. Only the declared comparison
  axes differ. These results may support Pareto and non-inferiority claims.
- **`contextual`:** The result uses compatible definitions but differs in an
  undeclared or non-primary factor, such as hardware, sample subset, concurrency,
  or cache state. It may support directional interpretation but not a controlled
  ranking.
- **`historical`:** The result predates the protocol, lacks required provenance,
  or uses incompatible conditions or metrics. Preserve it for context; exclude
  it from controlled comparisons and Pareto claims.

The report generator derives eligibility from validated fields. A prose claim
cannot promote a result to a stronger class.

## Run manifest and provenance

Before the first measured request, freeze a manifest containing:

- candidate ID, configuration digest, run ID, repository commit, protocol and
  schema versions, timestamp, and operator notes;
- host RAM, CPU, kernel, both GPU identities, driver/CUDA versions, GPU topology,
  storage device/filesystem, and free space;
- runtime/checkpoint revisions and hashes plus the rendered launch command or
  service configuration;
- evaluator/dataset revisions, selected sample IDs, agent/scaffold, scorer,
  seed, limits, concurrency, and timeout;
- requested and tokenizer-observed input/output lengths;
- artifact destinations and media types.

Operator notes explain anomalies; they do not replace typed configuration or
measurement fields.

## Context protocol

The protocol distinguishes:

- **Advertised context:** checkpoint or runtime claim; not a measurement.
- **Requested context:** target total model window submitted by the harness.
- **Observed input length:** tokenizer-measured input tokens.
- **Observed output length:** tokenizer- or server-measured output tokens.
- **Allocated context:** largest tested requested window that completed with the
  required reserve and without OOM, overflow, timeout, or protocol failure.
- **Effective context:** largest tested requested window that meets the accuracy
  thresholds below.

For target total window `C`:

```text
output_reserve = 4,096 tokens
input_budget = C - output_reserve
```

Construct the largest deterministic input that does not exceed `input_budget`.
Record the requested budget and observed tokenizer length separately; never call
an approximate character count a token count.

Test 128K, 256K, 512K, and 1,000K where claimed or technically credible. At each
allocated length, use the fixed RULER-style subset:

- single-needle retrieval;
- multiple keys and multiple needles;
- variable tracking or aggregation that cannot pass by one lexical match;
- eight insertion depths and three seeds per task and length.

Compute exact accuracy by task, length, depth, and seed before aggregation.
Effective context is the greatest tested length satisfying both:

```text
mean exact accuracy >= 0.90
worst depth-band accuracy >= 0.80
```

These thresholds are campaign qualification rules, not a general claim that all
applications find the window usable.

`capacity-qualified` means a completed 128K request with the output reserve.
`agent-viable` means effective context of at least 128K plus a 30-minute
growing-history/tool-call soak without OOM or protocol failure.

A shorter needle-in-a-haystack smoke may diagnose obvious breakage but cannot
establish effective context.

## Cache conditions

Every latency and resource sample records a primary cache condition:

- **`cold_model`:** the runtime/model has just started or loaded and no request
  has warmed model, expert, or page state;
- **`warm_model_cold_prefix`:** the model is warm and the tested prefix has not
  been used in the declared cache lifetime;
- **`warm_model_warm_prefix`:** the model is warm and backend telemetry confirms
  a prefix-cache hit;
- **`steady_state`:** the declared runtime-specific warmup criterion has been
  met and the manifest records that criterion.

When telemetry cannot confirm a prefix hit, use `warm_model_cold_prefix` or mark
the prefix state unavailable; elapsed time alone does not prove a hit.

Storage/expert-offload runs additionally record `cold_storage`, `warm_storage`,
or `not_applicable`, plus the cache-reset method and time to steady state. Cold
and warm results remain separate; steady-state results never replace cold-start
behavior.

## Admission and API behavior

Before capacity or quality measurement, verify:

- model load and checkpoint completeness;
- tokenizer and chat template;
- streaming termination and finish reasons;
- content and reasoning fields;
- single, multiple, parallel, and multi-turn tool calls;
- reasoning/tool state through a complete tool round trip;
- context-limit errors.

Classify failures as checkpoint, runtime support, startup OOM, allocation OOM,
context overflow, protocol/API, parser, timeout, crash, invalid tool JSON, or
incorrect result. Do not collapse these into a generic failure count.

## Latency protocol

Use monotonic timestamps. Dispatch time is captured immediately before the
client sends the request.

```text
TTFT = first content or reasoning token timestamp - dispatch timestamp
end_to_end = completion timestamp - dispatch timestamp
ITL[i] = token timestamp[i] - token timestamp[i - 1]
decode_tokens_per_second = emitted output tokens /
    (completion timestamp - first emitted token timestamp)
```

Count tokenizer- or server-observed tokens, never stream chunks. Publish p50 and
p95 over request summaries and over the relevant inter-token deltas. When useful,
report reasoning and content decode separately without hiding combined output.

Report prefill throughput only when a controlled one-token-output workload or
server telemetry isolates it. Do not infer prefill by subtracting unrelated
client timings.

At 8K, 32K, 128K, and every larger qualified context, measure one concurrent
request with fixed 256-token generation, temperature zero, and a fixed reasoning
mode:

- 20 warm repetitions at 8K and 128K;
- five warm repetitions at larger contexts;
- three cold starts.

## Resource protocol

Capture, where exposed:

- per-GPU peak and time-weighted VRAM, utilization, clocks, power, temperature,
  and PCIe traffic;
- process anonymous and file-backed RSS;
- system page cache, CPU utilization, major faults, swap, and pressure stall
  information;
- storage read bytes, latency, and queue depth;
- runtime prefix/expert cache state and capacity.

Unavailable telemetry is recorded as unavailable, never zero.

For storage-bound runs, additionally report bytes read and major faults per
generated token, cold/warm TTFT, warmup curve, expert-cache data when exposed,
and steady-state decode throughput. Energy is secondary telemetry and is not a
promotion gate.

## Quality and reliability

Use official scorers and `pass@1`; never substitute `pass@k`.

```text
context_exact_accuracy = correctly scored samples / attempted samples
coding_or_tool_pass_at_1 = official scorer successes / attempted tasks
reliability_rate = successfully completed attempts / all attempts
```

Report timeout, crash, OOM, invalid tool JSON, parser, and context-overflow rates
separately. An attempted task that fails operationally remains in the reliability
denominator and is not silently removed from the quality record.

For proportions, report a Wilson 95% interval with `z = 1.96`:

```text
(p + z²/(2n) ± z·sqrt(p(1-p)/n + z²/(4n²))) / (1 + z²/n)
```

Preserve scorer variants separately. A permissive diagnostic regrade does not
replace the canonical scorer result.

Agent time to solution is wall time from task start until the official scorer can
evaluate the final result. Also report model-wait time, turns, tool calls, and
token counts.

## Evaluator and dataset pinning

Every evaluator run records:

- evaluator repository and exact revision;
- dataset release or snapshot and selected sample IDs;
- scorer implementation and version;
- prompt, chat template, agent/scaffold, and tool schemas;
- sampling and reasoning settings;
- seed, concurrency, task limits, message limits, and timeout;
- evaluator environment lockfile, image digest, or Bazel/Nix revision;
- backend endpoint and API shape.

Sample selection is deterministic from stable IDs and a recorded seed. Dataset
row order and global random state are not inputs.

The initial quality funnel is the fixed LiveCodeBench subset, fixed BFCL subset,
and canonical pinned Inspect SWE-bench Verified pilot defined in <PLAN.md>.
Other runners are diagnostics until explicitly admitted to a protocol version.
Haku is a downstream compatibility and soak workload, not the primary score.

## Artifacts and immutability

Raw Inspect logs, evaluator work directories, traces, and telemetry live in the
private `llm-evals` S3 bucket. Each artifact reference records:

- URI;
- lowercase hexadecimal SHA-256;
- byte size;
- media type.

The harness verifies remote metadata after upload. Credentials never appear in
manifests, summaries, logs, or committed configuration.

A run directory becomes immutable when its result is accepted. Corrections,
reruns, and alternate scoring create a new derived run. Accepted artifacts and
summaries are never overwritten.

## Comparison and reporting

Do not calculate a weighted best-model score. A configuration is dominated only
when another controlled result is no worse in effective context, quality,
latency, and relevant resource cost and is strictly better in at least one.

Generate views for effective-context ceiling, coding quality at or above 128K,
128K latency, 1M attempts, offload quality ceiling, and quality versus p95 agent
time to solution. Contextual and historical results remain visible but cannot
dominate controlled results.

`results.md` is generated from validated registry entries and accepted summaries.
Do not append current benchmark numbers to documentation by hand.

## Validation

The checked-in harness must test:

- schema rejection and stable IDs/digests;
- exact token-budget construction and output reservation;
- streaming timing, finish reasons, reasoning, and tool events;
- metric formulas and Wilson intervals;
- artifact hashing, upload metadata, and resumable/idempotent runs;
- deterministic sample selection and report generation;
- cache labels that cannot be omitted or conflated.

Use a fake OpenAI-compatible server for streaming and failure paths. Give every
external evaluator a one-sample end-to-end smoke before a campaign. Re-running a
manifest must select the same configuration and dataset IDs.
