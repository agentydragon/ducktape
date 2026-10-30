# Anthropic compaction compatibility, 2026-10-30

The organization's API models support server-side compaction, but the running LiteLLM
1.103.0 silently drops the newer on-demand `compaction` request parameter. Threshold
compaction has request-path support. The existing BuildBuddy testing key allows only
Haiku 4.5 among paid Anthropic models, and that model does not support compaction.

## Live model discovery

A read-only `GET https://api.anthropic.com/v1/models?limit=100`, with
`anthropic-version: 2023-06-01` and `anthropic-beta: compact-2026-09-04`, used the
existing organization credential inside the LiteLLM Pod. No credential was printed.
The exact IDs underlying the catalog's paid API routes were all present:

| Model ID                    | `capabilities.compaction.supported` | `capabilities.compaction.summarize.supported` |
| --------------------------- | ----------------------------------- | --------------------------------------------- |
| `claude-opus-5`             | `true`                              | `true`                                        |
| `claude-sonnet-5`           | `true`                              | `true`                                        |
| `claude-fable-5`            | `true`                              | `true`                                        |
| `claude-haiku-4-5-20251001` | `false`                             | `false`                                       |

These are organization API models. `cluster/cdk8s/litellm/upstreams.py` binds
`ANTHROPIC_API` to `ANTHROPIC_API_KEY` with no alternative API base; subscription
routes have a separate binding to CLIProxyAPI.

## Running proxy

Both LiteLLM replicas used:

- Image: `git.allegedly.works/ducktape-ci/tana-litellm-proxy:devel-20261028093825-8b61b5d`.
- Image digest: `sha256:3f869f3b592b8ee1e4bfe71e8bbdc33ee482b4e10e3db8407e8484f5b256673f`.
- Installed package version: `1.103.0`, matching `MODULE.bazel`.

Credential-free synthetic probes ran against the installed package in
`litellm-59b569d46b-4zkds`. They exercised
`AnthropicMessagesRequestUtils.get_requested_anthropic_messages_optional_param`,
`AnthropicMessagesConfig.transform_anthropic_messages_request`, the beta-header
filter, and the native non-streaming response transformation.

| Input or behavior                                                                                     | Observed result                                                                                                   |
| ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Top-level `compaction: {"type": "summarize"}`                                                         | Silently omitted from the constructed upstream body, even with `drop_params=False`                                |
| `extra_body.compaction`                                                                               | Not retained by the optional-parameter filter; the native Messages HTTP path has no subsequent `extra_body` merge |
| `context_management.edits` with `compact_20260112`, trigger 50,000 and `pause_after_compaction: true` | Preserved in the constructed upstream body                                                                        |
| Threshold compaction edit                                                                             | Adds `compact-2026-01-12` to the beta header                                                                      |
| Explicit `compact-2026-09-04` beta                                                                    | Survives the final direct-Anthropic beta-header filter                                                            |
| Synthetic signed compaction block in subsequent input                                                 | Preserved by request transformation                                                                               |
| Synthetic compaction response, including signature, `stop_reason: compaction`, and `usage.iterations` | Preserved by native non-streaming response transformation                                                         |

The proximate on-demand blocker is the optional-request-parameter vocabulary:
`AnthropicMessagesRequestOptionalParams` lacks `compaction`, and the filter only
retains its declared keys. The native HTTP handler builds its body from that filtered
result. Merely sending the beta header therefore does not request compaction.

Source pointers at the public `v1.103.0` commit
`c991f4b01f5799eb0b0cab0fb63988e15c3a8a9d`:

- [Request vocabulary](https://github.com/BerriAI/litellm/blob/c991f4b01f5799eb0b0cab0fb63988e15c3a8a9d/litellm/types/llms/anthropic.py#L393).
- [Optional-parameter filter](https://github.com/BerriAI/litellm/blob/c991f4b01f5799eb0b0cab0fb63988e15c3a8a9d/litellm/llms/anthropic/experimental_pass_through/messages/utils.py).
- [Native request and response transformation](https://github.com/BerriAI/litellm/blob/c991f4b01f5799eb0b0cab0fb63988e15c3a8a9d/litellm/llms/anthropic/experimental_pass_through/messages/transformation.py#L476).
- [Native HTTP request construction](https://github.com/BerriAI/litellm/blob/c991f4b01f5799eb0b0cab0fb63988e15c3a8a9d/litellm/llms/custom_httpx/llm_http_handler.py#L2221).

Agentplane's ingress forwards the body without translation and its header filter does
not remove `anthropic-beta`. The remaining central-sampler work still includes native
Messages continuation, compaction command handling, context replacement, and History.

## Testing credential

The live `litellm/key-buildbuddy-live-tests` resource
(`keys.litellm.m.crossplane.io`) reported `Ready=True`, `Synced=True`. Its only
`anthropic-*` model was
`anthropic-api/ant-messages/claude-haiku-4-5-20251001`; its declared budget was $50
per 30 days. This verifies the managed resource, not BuildBuddy secret delivery or
remaining spend. No secret value was retrieved.

Add a compaction-capable paid API route, such as
`anthropic-api/ant-messages/claude-sonnet-5`, specifically to the BuildBuddy key.
Changing the shared `cheap_experiments_models` lane would also widen the separate
`cheap-experiments` key and is unnecessary for this acceptance test.

## Consequences for implementation

1. Keep on-demand server compaction as the preferred primitive for both explicit
   `Compact` and sampler-triggered automatic compaction. It has no 50,000-token
   trigger minimum and permits a small real-model acceptance conversation.
2. First add verified LiteLLM support for its top-level request parameter. A newer
   release is a candidate only after the same request-path check passes; no upgrade
   target was established in this investigation.
3. Preserve the signed block exactly, replace the summarized input, and send the
   on-demand beta on subsequent requests. Compact only after outstanding tool calls
   are settled. Preserve standing instructions and retained History independently.
4. Run a real BuildBuddy test that samples, executes a tool, compacts, restarts the
   sampler, resumes from the signed block, and executes another tool. Include an
   automatic-trigger case and correct `usage.iterations` accounting.

Anthropic documents
[on-demand compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand)
and [threshold compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold).
The two protocols use different history placement and cannot be combined on one
request. Threshold compaction's supported trigger minimum is 50,000 input tokens.

No generation or compaction request was sent to a real model. This investigation
confirms live model capabilities and installed proxy transformation behavior; it
does not establish end-to-end compaction, streaming accounting, or BuildBuddy live-test
success. No deployment or credential policy was changed.
