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

## Initial live results

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

Moving all system messages to the beginning would change their position and is
not the proposed remedy. Ollama's `/api/create` template field changes its Go
template layer, while this native Jinja path reads `tokenizer.chat_template` from
the GGUF. The proposed change derives a separate copy of the small metadata shard,
rendering later system/developer messages at their original positions. Original
shards and weight tensor bytes must remain unchanged.

The derived SSD artifact was produced with the Bazel patcher in
[invocation eb49ef25](https://app.buildbuddy.io/invocation/eb49ef25-dd52-4cfd-a8da-28f0620a4db3).
Both original and derived first shards are 10,946,624 bytes; the GGUF header reports
zero tensors and 67 metadata entries. Original SHA256 remains
`5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4`;
derived SHA256 is
`8681e217aad3be934fd9542709bf44123c4b569cb7906c01a2a383ac79c7c05f`.
It lives in `agentplane-midturn/` under the existing IQ4 SSD directory. Creation
alone does not establish that the running Ollama model uses this template.

The capture files are local and private; full prompts and tool schemas are not
committed. The capture tool's zero exit status means capture completed, not that
the model task passed.

## Context configuration and remaining acceptance

The initial deployment supplied no route-specific context override to either
harness. Backend `num_ctx` alone does not tell a harness when to compact. The
runner change must set the verified 131072/262144-token budget for each Qwen alias
on initial launch and resume. Model switches across differing configured budgets
must require a new thread unless the native process can safely update its budget.

Codex 0.156.1's [model-info override](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/models-manager/src/model_info.rs#L16-L27)
applies `model_context_window`. With no separate explicit compaction limit, its
[model protocol](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/protocol/src/openai_models.rs#L484-L506)
derives the automatic-compaction limit as 90% of that resolved window. Therefore
the runner should set the window and retain Codex's native compaction policy.
Its [provider capability selection](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/model-provider/src/provider.rs#L372-L388)
does not enable remote compaction for our custom `Agentplane LiteLLM` provider;
the turn loop uses local compaction through normal inference requests, without
requiring a `/responses/compact` endpoint.

The context configuration merged in [PR #8263](https://github.com/agentydragon/ducktape/pull/8263).
Its released wheel is `agentplane-runner-42b857721ffb`; the live SandboxTemplates
received the mapping before the new runner image finished publishing. The
[template registration change](https://github.com/agentydragon/ducktape/pull/8266)
also merged with required CI green.

The first registration rollout exposed a separate lifecycle problem. At
2026-09-28 00:54:31 UTC, Reloader reacted to `gpt-oss-scripts` and reported updating
both `setup-gpt-oss-v6` and the newly created `setup-gpt-oss-v7`. Their pods were
deleted. More than six minutes later, v7 still had no pods and no completed or
failed count. Ollama itself recovered to 2/2 Ready, but its Qwen manifest digests
remained unchanged. The follow-up excludes this explicitly versioned Job from
Reloader and advances its version for a clean registration attempt.

Repository fixture pins and runner-image package versions differ: the inspected
fixture pins are Claude 2.1.252 and Codex 0.152.0; the locked Nix image evaluates
to Claude 2.1.280 and Codex 0.156.1. Verify the actual deployed binaries during the
next owned acceptance sandbox, rather than assuming fixture versions are live.

After the metadata-template and runner changes deploy, run both harnesses through
both route families at both context sizes, serially. Capture native reported
context budgets alongside tool outputs. Successful short tasks and reported
budgets will not establish long-context quality or observed compaction.

```bash
bazelisk test //agentplane/acceptance:test_ollama_routes \
  --test_output=streamed --nocache_test_results --test_arg=-s \
  --test_arg=-k --test_arg=iq4xs
```

This selects eight cases. Preserve each run's undeclared test outputs before
rerunning, since the Bazel output paths are reused.
