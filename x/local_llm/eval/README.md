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

The interactive `rugged-opencode-offline` launcher uses host networking and
connects directly to Ollama on loopback. The word “offline” describes its local
model route; the launcher does not isolate OpenCode from other network access.
`offline_exec` and the historical fully isolated task runner retain their
network-isolation behavior.

On Rugged, the `rugged-opencode-offline` provider exposes six tested Ollama
models in OpenCode's model picker. The display names include the backend
context allocation. `RUGGED_OPENCODE_MODEL` still chooses the starting model
and keeps `qwen3-4b` as its default; for example:

```bash
RUGGED_OPENCODE_MODEL=gemma4-e2b-long-128k-experimental rugged-opencode-offline
```

The model roster and capability labels are:

| Selector                            | Ollama model ID                    | Context | Tool calls            |
| ----------------------------------- | ---------------------------------- | ------: | --------------------- |
| `qwen3-4b`                          | `qwen3:4b-instruct-2507-q4_K_M`    |  32,768 | verified              |
| `gemma4-e2b`                        | `gemma4:e2b-it-qat`                |  32,768 | verified              |
| `gemma4-e2b-long-128k-experimental` | `gemma4:e2b-it-qat-long-128k-b128` | 131,072 | short tool smoke only |
| `gemma4-e4b`                        | `gemma4:e4b`                       |  32,768 | verified              |
| `qwen2.5-coder-7b`                  | `qwen2.5-coder:7b`                 |  32,768 | unverified            |
| `qwen3-8b`                          | `qwen3:8b`                         |  32,768 | unverified            |

The two speed-only candidates remain selectable with `tool_call=false` in
their OpenCode model metadata. A Qwen3 8B mock request still carried the six
local agent-tool schemas, so this metadata does not prevent a generated tool
call; successful tool use remains unverified. OpenVINO NPU and LiteRT models
are not in this Ollama-only picker because this launcher has no route to those
separate backends.

The picker does not pull weights or prove that a tag is cached. Rugged's
NixOS host declares prefetch for the five tested base tags in the table;
applying that generation downloads missing weights while network access is
available, after which local launcher runs can stay offline. The experimental
128k alias is created separately from its Gemma E2B base by serving PR #10491.
Check `OLLAMA_HOST=http://127.0.0.1:11436 ollama list` before selecting a
model, especially the alias.

The experimental selector routes to `gemma4:e2b-it-qat-long-128k-b128` and
advertises 131,072 context tokens with a 4,096-token output limit. The alias is
prepared with 131,072 context and batch 128. Match the qualification run by setting
`OLLAMA_KV_CACHE_TYPE=f16` and `OLLAMA_FLASH_ATTENTION=0` on the Ollama service
before loading the model; the offline launcher does not configure server-wide
settings. This remains an experiment:
the long retrieval trial processed 124,005 uncached input tokens, but only one
of three exact-retrieval checks passed. A large accepted context therefore does
not imply reliable long-context coding. The default selector remains Qwen 4B.
Details are tracked in the
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
