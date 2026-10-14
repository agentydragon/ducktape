# Rugged offline coding harness and local coding samples

This run records a local-only OpenCode profile, an isolated end-to-end coding
task, and a fixed HumanEval+ comparison cohort. It is a workstation sample, not
a full HumanEval+ leaderboard result or a Terminal-Bench score.

## Offline OpenCode profile

The Home Manager host module `nix/home/hosts/rugged-opencode.nix` adds the
`rugged-opencode-offline` command. It copies a minimal OpenCode config into a
fresh writable home, disables plugins and MCP, disables delegation, and permits
only local file and shell tools. OpenCode runs inside `bubblewrap
--unshare-net`; its only model connection is a temporary mode-0600 Unix socket
forwarded to one fixed host-loopback Ollama port. Set
`RUGGED_OPENCODE_BACKEND_PORT` to override the default `11436`. The command
expects Ollama and the selected model to be available before it starts.
`RUGGED_OPENCODE_MODEL` selects from the declared `qwen3-4b`, `gemma4-e2b`,
`gemma4-e4b`, or `gemma4-e2b-long-128k-experimental` profiles; Qwen remains
the default. Tool use is enabled for E4B only after the successful 3-repeat
probe below.

The experimental long-context profile routes to the serving alias
`gemma4:e2b-it-qat-long-128k-b128` with 131,072 context and a 4,096-token
output limit. The actual built launcher passed a mock wire/config check and a
short live tool roundtrip; its exact generated config (Base64 archived with a
decoded SHA256 manifest) and request/response artifacts are in
`results/opencode-long128-experimental/`. The live request read a
one-line marker through the `read` tool and returned the exact marker. Both
OpenAI-compatible requests returned HTTP 200 with the server using F16 KV,
flash attention off, context 131,072, and batch 128. The separate long
retrieval trial processed 124,005 uncached input tokens but passed only one of
three exact-retrieval checks, so this profile is experimental and is not a
quality recommendation. Existing profiles and the default are unchanged.

The actual launcher passed a mock OpenAI-compatible wire check. The recorded
request carried `reasoning_effort: none`, temperature `0.2`, `top_p: 0.95`,
and the six local tools `bash`, `edit`, `glob`, `grep`, `read`, and `write`.
The mock artifacts are under `results/built-launcher-wire-24031/`.

## End-to-end task

The runner `smoke_7910/run_fully_offline_task.py` starts the cached Ollama model
and the actual OpenCode launcher in one outer network namespace. Its evidence
records a successful loopback `/api/tags` request, a failed public TCP probe,
Vulkan device selection, and model layer offload before the coding prompt is
sent. The model cache is read-only and `OLLAMA_NO_CLOUD=1` is set.

For run `qwen3-4b-offline-jsonl-4`, Qwen3 4B completed the PR #7910
`jsonl-transform` task in 77 seconds and left the task tests unchanged. It ran
the requested original unittest command, which reported two failures; the model
then claimed the tests passed without repairing the implementation. Its final
response is therefore not counted as task success. The separate Bazel-invoked
frozen-verifier result is recorded alongside that run. In `repair-5`, the same
agent workspace was continued with the exact failures; the agent edited only
the implementation, and the independent Bazel verifier passed all four tests
in 179 seconds of agent time. The task tests remained byte-identical. The
runner records public egress denied while cached Ollama stayed reachable,
Vulkan selected Intel LNL, and all 37 model layers offloaded. The initial
77-second attempt and successful 179-second continuation are reported
separately; their sum is not total resolution time because earlier setup-invalid
and interrupted attempts are excluded. `attempt-chain.json` records those
attempts, including the valid-CWD repair that was manually interrupted before
the successful continuation. The earlier
`qwen3-4b-offline-jsonl-3` attempt is retained as an interrupted CPU fallback:
Ollama dropped the integrated GPU because the runner had omitted
`OLLAMA_IGPU_ENABLE=1`.

## Frozen HumanEval+ cohort

The cohort samples 20 of the 164 EvalPlus HumanEval tasks without replacement
using seed `20261013`, then runs three generations per task. IDs, prompt bytes,
generation options, seeds, EvalPlus version `0.3.1`, and HumanEval+ release
`v0.1.10` are pinned in `humaneval_plus_cohort.json`,
`humaneval_plus_prompts.jsonl`, and `humaneval_plus_prompts_metadata.json`.
The prompt file SHA256 is
`6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd`.
Generation requests use temperature `0.2`, `top_p: 0.95`, up to 2,048 output
tokens, and repeat seeds `20261013`–`20261015`. Results use EvalPlus's upstream
sanitizer and original plus augmented verifiers, reported separately as Base
and HumanEval+.

The NPU Qwen2.5-Coder 1.5B INT4 cohort is stored in the sibling
`2026-10-13_rugged_npu_openvino` run; this directory contains its scored
derivatives under `results/npu-qwen2.5-coder-1.5b-openvino/evalplus/`. Its
context was 8,192 tokens; the iGPU cohort context is 32,768 tokens. These
context and backend differences are part of the comparison.

| Candidate                        | Base pass@1 | HumanEval+ pass@1 | Samples | Status                             | Context |
| -------------------------------- | ----------: | ----------------: | ------: | ---------------------------------- | ------: |
| Qwen3 4B Instruct, Ollama Vulkan |       0.950 |             0.900 |   60/60 | 60 ok                              |  32,768 |
| Gemma 4 E4B, Ollama Vulkan       |       1.000 |             0.967 |   60/60 | 60 ok; 3 reached output limit      |  32,768 |
| Qwen2.5-Coder 1.5B, OpenVINO NPU |       0.683 |             0.650 |   60/60 | 60 ok                              |   8,192 |
| Gemma 4 E2B QAT, Ollama Vulkan   |           — |                 — |   25/60 | Paused at user direction; unscored |  32,768 |

Per-sample pass/fail counts, timing distributions, first-repeat timings,
memory sampling coverage, and peak RSS/GTT are recorded in each candidate's
`performance-summary.json`. Ollama `/api/chat` does not expose client TTFT;
the summary does not infer it. Prefix-cache contribution is not separately
reported, so prompt counts are not divided by prefill duration to claim a
prefill rate.

The E4B OpenAI-compatible tool probe is recorded at
`results/gemma4-e4b-igpu/openai-tool-probes-short-path.jsonl`: all three first
responses called `read` on logical path `main.py`, the supplied fixture matched
exactly, and each follow-up returned `identify_value`. The earlier probe with a
long absolute path and 64-token limit remains in `openai-tool-probes.jsonl` as
a failed protocol attempt; it is not counted as tool-use evidence.

The Gemma 4 E2B Ollama cohort was paused after its 25th response when the
priority changed to long-context work. The raw file and request configuration
remain in `results/gemma4-e2b-igpu/`; there is no EvalPlus score for this partial
cohort. Its `*.inflight.json` names the next unissued request. The model was
unloaded before releasing the inference slot. No LiteRT cohort was started.
The OpenAI-compatible runner remains available for later LiteRT comparisons and
preserves raw responses, usage, finish reason, and requested seed; seed
acceptance remains unknown when the endpoint does not report it.

The reusable runner and namespace wrapper are owned by `x/local_llm/eval/`.
For a repeat, pass this run's frozen manifest and prompts explicitly to
`bb run //x/local_llm/eval:run_ollama_cohort`. Run the client through
`bb run //x/local_llm/eval:offline_exec` and point it at the forwarded loopback
port. Score a complete raw cohort with
`x/local_llm/eval/score_humaneval_plus_cohort.py --cohort
humaneval_plus_cohort.json --workers 1` inside the pinned EvalPlus environment;
keep raw output and scored derivatives separate. The runner records requested
options, model digest, native token counts and durations, finish reasons,
memory snapshot, and power state. Task-major repeats may benefit from
prefix-cache reuse, so report all-sample timing separately from
first-repeat-per-task timing and do not treat these generation timings as
isolated prefill benchmarks.

`smoke_7910/run_repair_continuation.py` performs one bounded repair in the same
failed task workspace. It snapshots the inherited implementation, supplies the
exact verifier failures, asks the model to rerun the entire original test suite,
and records per-attempt agent time. `attempt-chain.json` distinguishes valid,
invalid-path, interrupted, and successful attempts; the independent Bazel
verifier remains the final authority for the repaired workspace.

Model-produced Python snapshots are archived as Base64 text in `.py.txt.b64`
files. Decoding reproduces the exact original bytes, including trailing spaces
that repository formatting hooks would otherwise remove. The
`source-archive-manifest.json` under
`smoke_7910/results/qwen3-4b-offline-jsonl-4/` maps the baseline, repair-5
input, and verified final implementation and test snapshots to their recorded
SHA256 values of the decoded source. To materialize an implementation snapshot,
run `base64 -d jsonl_transform.py.txt.b64 > jsonl_transform.py`; decode test
snapshots similarly, then compare their hashes with the manifest before running
the independent verifier.

## LiteRT-LM 0.17.0 long-context capability notes

The installed server is LiteRT-LM 0.17.0. Its OpenAI `serve` command has no
`--max-num-tokens` flag. The request's `model` field accepts the backward-
compatible form `<model-id>,<backend>,<max_num_tokens>`, so a request can ask
for `gemma4-e2b-it,gpu,131072`. The `max_num_tokens` setting is the KV-cache
budget for the sum of prompt and generated tokens; it is a requested engine
setting and does not prove the cached graph can use the full value. The server
also accepts this field through per-model `config.json` configuration.

On this installed 0.17.0 build, `litert-lm describe gemma4-e2b-it` reports
model capabilities but no maximum context. `litert-lm-peek` shows the cached
bundle's LiteRT-LM file version 1.5.0 and its `tf_lite_prefill_decode` graph,
but no context-capacity field. Thus the actual cap for this exact cached bundle
is unknown until tested. The endpoint returns prompt/completion token counts in
`usage`; its `finish_reason` is `stop` except for tool calls, so that field alone
cannot confirm a response ended because it reached its output limit. Use
`max_completion_tokens` to bound output; this handler ignores `max_tokens`.

The server does not expose prefill chunk-size controls. The v0.17 C++ executor
has `AdvancedSettings.prefill_batch_sizes` and a CPU-only `prefill_chunk_size`
setting, but the CLI server only configures the model/backend, `max_num_tokens`,
and ring-buffer local attention. The model's prefill graph determines which
prefill signatures can run.

A later upstream v0.18.0 release adds model introspection and static
`max_num_tokens` clamping; those features are not present in the installed
0.17.0 CLI. See the [LiteRT-LM CLI guide](https://developers.google.com/edge/litert-lm/cli),
[v0.17 executor settings](https://github.com/google-ai-edge/LiteRT-LM/blob/v0.17.0/runtime/executor/llm_executor_settings.h),
[v0.18.0 release notes](https://github.com/google-ai-edge/LiteRT-LM/releases/tag/v0.18.0),
and [upstream context-cap issue #3444](https://github.com/google-ai-edge/LiteRT-LM/issues/3444).

For a future isolated test, bind only to loopback:

```bash
/tmp/rugged-litert-wrapped/bin/litert-lm serve \
  --host 127.0.0.1 --port 9379 --enable-speculative-decoding=true
```

Send progressively longer, precisely tokenized prompts to
`/v1/chat/completions`, with model `gemma4-e2b-it,gpu,131072`,
`max_completion_tokens` set to a small fixed count, and `stream: false` for
token-count validation. Record the returned `usage.prompt_tokens` and
`usage.completion_tokens`, HTTP success/error, client wall time, server logs,
process RSS/HWM, deduplicated DRM GTT residency, `MemAvailable`, and memory
pressure. Treat an accepted `131072` setting as configuration only; claim
working 128k context only after a near-limit prompt plus generation completes
with the returned token sum within budget. No long-context inference was run as
part of this read-only investigation.
