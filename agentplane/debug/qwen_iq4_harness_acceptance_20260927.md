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

The capture files are local and private; full prompts and tool schemas are not
committed. The capture tool's zero exit status means capture completed, not that
the model task passed.

## Context configuration and remaining acceptance

The initial deployment supplied no route-specific context override to either
harness. Backend `num_ctx` alone does not tell a harness when to compact. The
runner change must set the verified 131072/262144-token budget for each Qwen alias
on initial launch and resume. Model switches across differing configured budgets
must require a new thread unless the native process can safely update its budget.

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
