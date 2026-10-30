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

For the initially evaluated LiteLLM-based acceptance option, add a compaction-capable paid API route, such as
`anthropic-api/ant-messages/claude-sonnet-5`, specifically to the BuildBuddy key.
Changing the shared `cheap_experiments_models` lane would also widen the separate
`cheap-experiments` key and is unnecessary for this acceptance test.

## Consequences for implementation

1. Keep on-demand server compaction as the preferred primitive for both explicit
   `Compact` and sampler-triggered automatic compaction. It has no 50,000-token
   trigger minimum and permits a small real-model acceptance conversation.
2. A proxy-based route would need verified LiteLLM support for its top-level request
   parameter. The selected direct Anthropic adapter bypasses this filter; a LiteLLM
   upgrade is not a prerequisite for that route.
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

The compatibility probe above sent no generation or compaction request. It established model
capabilities and installed proxy behavior only. The subsequent implementation acceptance below
establishes end-to-end generation, compaction and BuildBuddy live-test behavior. No deployment was
changed during either stage.

## Implementation acceptance

[PR #13085](https://github.com/agentydragon/ducktape/pull/13085) implements direct organization-paid
Anthropic Messages in the sampler. No staging deployment or subscription cutover was performed.
The existing organization key was authorized for bounded tests, injected only through ephemeral
redacted BuildBuddy headers. Persistent provisioning remains [#13063](https://github.com/agentydragon/ducktape/issues/13063).

- [All three real Haiku cases pass](https://app.buildbuddy.io/invocation/107cb976-2b78-440e-8b55-70265d3a6c61):
  JSON exec and actual nested shell execution; manual signed compaction with unchanged retained
  thinking, strict prefix validation, empty response transformations, sampler restart, fact recall
  and another tool call; cache creation then reads across a per-message effort change; automatic
  compaction followed by resumed sampling and recall. The target uses `claude-haiku-5-5`.
- [All six sampler test targets pass](https://app.buildbuddy.io/invocation/ac606445-cd9d-455e-91d2-5c4b02cb90da),
  including existing Responses behavior and real-Postgres native truncation/compaction atomicity.
- [The Go target passes with database fault injection](https://app.buildbuddy.io/invocation/08ff7386-7508-4568-87d8-335329726658):
  failed request evidence prevents provider dispatch; failed response evidence after a durable
  prefix leaves the dispatched intent unresolved without executable output.
- [Six chart/catalog/manifest/render targets pass](https://app.buildbuddy.io/invocation/1e3edcd3-2d1c-4ac7-b72d-4559fc323fa3).
  Changed-file pre-commit hooks pass. Generated manifests match the chart.

Live fixture configs now refer to private temporary credential files outside uploaded artifacts;
provider records omit authentication. The service stores native request/response content in its
own DB without requiring the integration app or a Langfuse exporter. Direct usage is billed to the
organization and does not inherit LiteLLM virtual-key budgets.
