# Agentplane LLM workload ingress

This is the authenticated Sandbox-facing hop in front of the existing LiteLLM deployment. The
central egress proxy substitutes the caller's already-authenticated Pod-bound workload token into
an ordinary `Authorization: Bearer` header. This service resolves that bearer with the shared
`WorkloadPrincipalAuthenticator`, removes it, and forwards the request to LiteLLM with one
server-held virtual key.

The forwarded byte body, status, error body, and streamed chunks are not translated. Verified
identity is attached only through LiteLLM's documented `x-litellm-spend-logs-metadata` JSON header
(deployed `litellm/litellm:1.100.0`, the `tana-litellm-proxy` image, `tana/litellm_proxy/BUILD.bazel`). That version
consumes the header (`_get_spend_logs_metadata_from_request_headers`) in its common request
setup used by both native `/v1/messages` and `/v1/responses`, including their streaming paths. The
authoritative metadata object is:

```json
{
  "agentplane.namespace": "...",
  "agentplane.service_account": "...",
  "agentplane.service_account_subject": "...",
  "agentplane.pod_name": "...",
  "agentplane.pod_uid": "...",
  "agentplane.sandbox_name": "...",
  "agentplane.sandbox_uid": "..."
}
```

Incoming LiteLLM metadata/customer/agent headers and Agentplane/Sandbox/Pod/Agent/Thread identity
headers are removed before this object is stamped. Request bodies remain provider-native and are
never identity evidence; a caller's body `metadata`, Agent, Thread, Pod, or Sandbox fields cannot
replace the server-stamped object.

The workload bearer is sent only to Kubernetes TokenReview. The LiteLLM virtual key is sent only on
the internal LiteLLM hop. Neither credential is logged, placed in errors, returned to callers, or
mounted into a runner or harness container.

## Per-model client configuration

The authenticated `GET /agentplane/model-config?model=...` endpoint returns a
`ModelConfig` record for an exposed route. The ingress retains complete records,
indexed by their exposed model IDs, rather than reducing them to integer budgets.
Lookups are answered locally and never forwarded to LiteLLM. An explicit 404
`"no configuration for model"` response lets the runner use harness defaults;
authentication, transport, malformed-response and other 404 errors fail the lookup.

Configuration is authored as a list in ingress settings, for example:

```yaml
models:
  - model: ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k
    total_context_budget_tokens: 131072
```

`total_context_budget_tokens` is the configured **input + output total** budget
for the harness, not separate maximum input or output tokens, an output request
cap, or verified provider capacity. Harness-specific reserves and compaction still
apply. Changing this policy should use a new route ID so retained sessions keep
matching their persisted budget. More client configuration fields can be added
when needed; no speculative options map is defined.

TODO(#9574): support an exposed model ID distinct from the LiteLLM model ID, e.g.
expose `x` to satisfy a harness's model-name expectations while forwarding inference
as `y`. Review request/response translation (including streaming), lookup identity,
authorization and accounting together. Current inference remains pass-through;
this TODO does not enable aliases or infer budgets/capabilities from a model name.

For targeted debugging, set `log_llm_requests: true` in the ingress settings (or
`AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=true`). The ingress logs each request body and every raw
response chunk with a per-request ID and chunk number, plus whether the stream completed. This can
include prompts, reasoning, generated text, and tool arguments; keep it disabled by default and
restrict access and retention while enabled. Authorization and other request headers are not logged.

Each environment projects selected routes through the explicit runner budget map
into `LlmIngressProps.models`, then `settings.py`'s configuration schema. `models.py` owns the lightweight
`ModelConfig` API contract shared by ingress, runner, and config generation.
These are configured harness overrides, not a catalog of every known model capacity.
