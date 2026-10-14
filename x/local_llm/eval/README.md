# Local coding evaluation tools

Reusable generation, isolation, memory-sampling, and task-verification tools
live here. Frozen prompts, model outputs, scores, and task fixtures stay under
`cluster/docs/inference/runs/` with their experiment records. For example, the
rugged HumanEval+ cohort inputs are
`cluster/docs/inference/runs/2026-10-13_rugged_coding/`.

The standard-library tools have Bazel targets:

```bash
bb run //x/local_llm/eval:offline_exec -- --workspace <workspace> -- <command>
bb run //x/local_llm/eval:run_ollama_cohort -- \
  --base-url http://127.0.0.1:11436 --model <local-model> \
  --cohort <frozen-cohort.json> --prompts <frozen-prompts.jsonl> --output <raw.jsonl>
bb run //x/local_llm/eval:run_openai_compat_cohort -- \
  --base-url http://127.0.0.1:9379/v1 --model <local-model> \
  --cohort <frozen-cohort.json> --prompts <frozen-prompts.jsonl> --output <raw.jsonl>
bb run //x/local_llm/eval:verify_generated_coding_task -- \
  <workspace> --result-json <verification.json>
bb run //x/local_llm/eval:summarize_humaneval_plus -- \
  --raw <humaneval-plus-raw.jsonl> --scores <sample-scores.jsonl> \
  --score-summary <summary.json> --memory <memory-samples.jsonl> \
  --candidate <model-and-backend> --output <performance-summary.json>
```

`offline_exec` starts the command in a fresh `bubblewrap --unshare-net`
namespace. By default, a temporary Unix socket forwards only to one fixed
host-loopback backend; `--no-backend` disables that bridge. Bubblewrap and
socat come from the repository Nix environment. For an offline proof that
includes the model server, start the cached server inside the same outer
network namespace as the launcher, as the rugged run script does.

On Rugged, the existing `rugged-opencode-offline` launcher accepts an explicit
experimental long-context selector:

```bash
RUGGED_OPENCODE_MODEL=gemma4-e2b-long-128k-experimental rugged-opencode-offline
```

It routes to the serving alias `gemma4:e2b-it-qat-long-128k-b128` and advertises
131,072 context tokens with a 4,096-token output limit. The alias is prepared
with 131,072 context and batch 128. Match the qualification run by setting
`OLLAMA_KV_CACHE_TYPE=f16` and `OLLAMA_FLASH_ATTENTION=0` on the Ollama service
before loading the model; the offline launcher does not configure server-wide
settings. This remains an experiment:
the long retrieval trial processed 124,005 uncached input tokens, but only one
of three exact-retrieval checks passed. A large accepted context therefore does
not imply reliable long-context coding. The normal selector and model profiles
remain unchanged. Details are tracked in the
[Rugged local-model report](../../../cluster/docs/inference/rugged_local_llm.md).

The built launcher passed both a mock-wire check and a short live tool
roundtrip using this selector. Its generated configuration (archived byte for
byte as Base64) and request artifacts are in
[`results/opencode-long128-experimental/`](../../../cluster/docs/inference/runs/2026-10-13_rugged_coding/results/opencode-long128-experimental/).
Decode `generated-config.json.txt.b64` and check its SHA256 against
`generated-config-manifest.json` to reproduce the exact generated config.
The live call read a marker file through OpenCode's `read` tool and returned
the marker exactly. The server reported HTTP 200 for both the tool request and
follow-up, with a 131,072-token context, F16 KV cache, flash attention off, and
batch 128. This is a tool/configuration smoke only; it does not add evidence
about long-context retrieval quality.

`run_ollama_cohort` records native Ollama `/api/chat` requests and raw
responses, including token counts, durations, stop reasons, model digest,
memory snapshot, and power state. Its prompt and manifest inputs are required
so each run explicitly names the frozen sample and generation settings.
`run_openai_compat_cohort` applies the same frozen chat messages and records
the OpenAI-compatible response, usage, finish reason, requested seed, and
endpoint-returned model metadata. It uses `max_completion_tokens`; whether the
endpoint honored sampling parameters such as `seed` remains unknown unless the
server exposes evidence.
`sample_ollama_memory` records process RSS/HWM and deduplicated DRM client GTT
values. `probe_openai_tool_calls` captures OpenAI-compatible request and
response bodies for bounded tool-call checks.
`summarize_humaneval_plus` reports raw status/reason counts, quality scores,
server timing and token distributions, first-repeat timing, and memory-sampler
peaks/coverage. It labels native Ollama timings separately from GenAI-reported
NPU throughput/TTFT and does not infer client TTFT for non-streaming calls. It
does not infer prompt-processing throughput from total prompt tokens and
prefill time because prefix-cache contribution is not reported separately.

`prepare_evalplus.py` and `score_humaneval_plus_cohort.py` consume the pinned
EvalPlus runtime recorded next to each experiment's data. They are exported in
the Bazel package but deliberately are not Bazel Python targets: EvalPlus's
large dependency graph and Python-version requirements do not belong in the
repository-wide Python lock. Use the experiment's pinned requirements lock and
run the scorer in its dedicated environment, with one worker when measuring a
shared workstation.
