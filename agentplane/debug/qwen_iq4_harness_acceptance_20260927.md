# Qwen IQ4_XS through deployed Agentplane harnesses

## Scope and baseline

The SSD-resident Qwen routes are already offered by both harness catalogs in
Agentplane staging and testing. Authenticated `/models` responses confirmed the
`ollama/{oai-chat,olm-chat}/qwen3.8-flash-next-iq4xs-{128k,256k}` entries.
The older HDD-resident Qwen Q4 entry is disabled in the shared roster; other
Ollama models remain offered.

These are deployment acceptance checks, not model-quality benchmarks. The earlier
[direct API checks](../../cluster/docs/inference/runs/2026-09-27_ollama_ssd/)
did not exercise the complete native harness prompts.

## Live acceptance: 2026-09-28 UTC

The [eight-case run](https://app.buildbuddy.io/invocation/f894b468-3d7e-4e03-b2cb-2eff48ee47d3)
finished in 536 seconds: six passed, two failed. Every passing case executed the
shell tool and returned both `/state/work` and `OLLAMA_SMOKE_TOOL_OK`.

| Harness | Route family | 128K   | 256K   | Native reported context (128K / 256K) |
| ------- | ------------ | ------ | ------ | ------------------------------------- |
| Claude  | `oai-chat`   | Passed | Passed | 131072 / 262144                       |
| Codex   | `oai-chat`   | Passed | Passed | 124518 / 249036                       |
| Claude  | `olm-chat`   | Passed | Passed | 131072 / 262144                       |
| Codex   | `olm-chat`   | Failed | Failed | No completed-turn evidence            |

Codex reports its effective usable window, 95% of the configured model window.
The native Ollama failures occur in LiteLLM 1.100.1 before inference:
`llms/ollama/chat/transformation.py:178` evaluates
`value in {"low", "medium", "high"}` with a dictionary reasoning value and raises
`TypeError: unhashable type: 'dict'`. Codex displays a generic high-demand error.
The Responses bridge retains the whole reasoning object when `summary` is present;
the native Qwen mapper expects a string effort. The same bridge defect was
[previously reproduced with GPT-OSS](agentplane_ollama_live_smoke_2026_09_24.md),
whose mapper forwards the object to Ollama instead of failing locally.
Use `oai-chat` for Codex. A native-adapter correction must preserve effort and
thinking semantics; dropping reasoning options is not equivalent.

Both environments advertise these routes. Actual tasks ran on testing with image
`devel-20260928010047-9a3565b`, wheel `agentplane-runner-42b857721ffb`.
The owned sandbox reported Claude 2.1.280 and Codex 0.156.1. Repository fixture
pins (Claude 2.1.252 / Codex 0.152.0) are not the deployed binary versions.

Short-response decode rates in the Ollama logs were approximately 26–57 tokens/s.
Cold alias loads took roughly 1.5–2.4 minutes including load and inference; warm
tool-result requests took roughly 1–3 seconds. These small requests do not measure
full-window latency, compaction, parallelism, or model quality.

Evidence is under `/tmp/agentplane-qwen-iq4-20260927/derived-template-eight-cases`
on `wyrm2`, with backend logs alongside it. All owned acceptance sandboxes were
cleaned up. Inference was serial throughout.

A private durable archive on `wyrm2` is
`/var/lib/llm-models-ssd/experiment-artifacts/2026-09-28-agentplane-qwen-iq4-harness-evidence.tar.gz`
(SHA256 `e06ba977e3b491dd8c795337a2111ae77259957e6ead41a78923a4f4efa8f386`).

## Original template regression

All cases ran serially against Agentplane testing. Each requested shell execution
of `pwd` and `printf 'OLLAMA_SMOKE_TOOL_OK\n'`, with recorded tool outputs required.

| Harness | LiteLLM route family | Context alias | Outcome                                                            |
| ------- | -------------------- | ------------- | ------------------------------------------------------------------ |
| Codex   | `oai-chat`           | 128K          | Passed: both shell outputs recorded; turn completed                |
| Claude  | `oai-chat`           | 128K          | Failed: Qwen template rejected a later system message              |
| Claude  | `olm-chat`           | 128K          | Failed: same template error                                        |
| Codex   | `olm-chat`           | 128K          | Failed: generic client diagnostic; matching backend template error |

The passing Codex thread was `db4cc77b-1835-4e9a-975d-da709998c375`.
BuildBuddy captured the [OAI-route run](https://app.buildbuddy.io/invocation/f8539b28-48ad-4b68-a315-298a7a6f60d4)
and [native Ollama-route run](https://app.buildbuddy.io/invocation/5efe6b81-391f-414b-9d82-3c4e5465ac82).
Local evidence is under `/tmp/agentplane-qwen-iq4-20260927/{oai128,olm128}` on wyrm2.

## Failure boundary

A header-blind capture using the repository-pinned Claude 2.1.252 binary recorded
an Anthropic Messages request with three top-level system blocks and message roles
`["user", "system"]`. LiteLLM 1.100.1 preserves that later system message when
translating to Chat Completions or Responses. Qwen's GGUF Jinja template accepts
contiguous initial system/developer messages but rejects those roles after an
ordinary message with `System message must be at the beginning.`

Moving all system messages to the beginning would change their position.
Ollama's `/api/create` template field changes its Go
template layer, while this native Jinja path reads `tokenizer.chat_template` from
the GGUF. The deployed change derives a separate copy of the small metadata shard,
rendering later system/developer messages at their original positions. Original
shards and weight tensor bytes remain unchanged.

The derived SSD artifact was produced with the Bazel patcher in
[invocation eb49ef25](https://app.buildbuddy.io/invocation/eb49ef25-dd52-4cfd-a8da-28f0620a4db3).
Both original and derived first shards are 10,946,624 bytes; the GGUF header reports
zero tensors and 67 metadata entries. Original SHA256 remains
`5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4`;
derived SHA256 is
`8681e217aad3be934fd9542709bf44123c4b569cb7906c01a2a383ac79c7c05f`.
It lives in `agentplane-midturn/` under the existing IQ4 SSD directory. Live
`/api/show` returned the checked-in Jinja template with SHA256
`ee4884b3b976b3d31b47e33420fdb29a324f12058a4436178690044e43eab96b`
(excluding the final newline), and `num_ctx` 131072 / 262144 for the two aliases.
The [canonical derivation command](../../cluster/k8s/ollama/README.md)
was also rerun idempotently in
[invocation 3d43c031](https://app.buildbuddy.io/invocation/3d43c031-cee0-4f5d-8a35-467ead87b171).

The capture files are local and private; full prompts and tool schemas are not
committed. The capture tool's zero exit status means capture completed, not that
the model task passed.

## Context configuration

The initial deployment supplied no route-specific context override to either
harness. Backend `num_ctx` alone does not tell a harness when to compact. The
runner now supplies the 131072/262144-token budget for each Qwen alias
on initial launch and resume: Claude receives `CLAUDE_CODE_MAX_CONTEXT_TOKENS`,
and Codex receives `model_context_window`. Model switches across differing
configured budgets require a new thread.

Codex 0.156.1's [model-info override](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/models-manager/src/model_info.rs#L16-L27)
applies `model_context_window`. With no separate explicit compaction limit, its
[model protocol](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/protocol/src/openai_models.rs#L484-L506)
derives the automatic-compaction limit as 90% of that resolved window. Therefore
the runner sets the window and retains Codex's native compaction policy.
Its [provider capability selection](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/model-provider/src/provider.rs#L372-L388)
does not enable remote compaction for our custom `Agentplane LiteLLM` provider;
the turn loop uses local compaction through normal inference requests, without
requiring a `/responses/compact` endpoint.

The context configuration merged in [PR #8263](https://github.com/agentydragon/ducktape/pull/8263).
The live SandboxTemplates and runner image both contain the change. The
[template registration change](https://github.com/agentydragon/ducktape/pull/8266)
also merged with required CI green.

## Registration lifecycle

Reloader deleted the initial registration Job pods after a script ConfigMap update.
The versioned Job now opts out with `reloader.stakater.com/auto: "false"`.
The HDD CSI driver also rejected simultaneous serving/registration mounts of
`llm-models` (`verifyMount: device already mounted`). The static SSD mount was
healthy. A temporary GitOps pause of serving released the HDD mount, allowing
`setup-gpt-oss-v8` to complete; serving then returned to one ready replica.
The completed Job has no TTL, preventing Flux from recreating it daily.
The serving SSD mount remains read-only.

The durable handoff procedure is in the
[Ollama README](../../cluster/k8s/ollama/README.md). Changes:
[Reloader exclusion](https://github.com/agentydragon/ducktape/pull/8270),
[temporary pause](https://github.com/agentydragon/ducktape/pull/8273),
[serving restoration and Job retention](https://github.com/agentydragon/ducktape/pull/8275).

## Reproduction and remaining checks

Run the harness cases serially. Successful short tasks and reported budgets do not
establish long-context quality or observed compaction. A longer continuation test
with an actual compaction remains useful after resolving the native adapter error.

```bash
bazelisk test //agentplane/acceptance:test_ollama_routes \
  --test_output=streamed --nocache_test_results --test_arg=-s \
  --test_arg=-k --test_arg=iq4xs
```

This selects eight cases. Preserve each run's undeclared test outputs before
rerunning, since the Bazel output paths are reused.
