# Agentplane LLM workload ingress

This is the authenticated Sandbox-facing hop in front of the existing LiteLLM deployment. Its Go runtime and Python deployment authors share the generated messages and Protovalidate
annotations in `configpb/config.proto`. The
central egress proxy substitutes the caller's already-authenticated Pod-bound workload token into
an ordinary `Authorization: Bearer` header. This service resolves that bearer with the shared
Go workload-auth resolver, removes it, and forwards the request to LiteLLM with one
server-held virtual key.

The forwarded byte body, status, error body, and streamed chunks are not translated. Verified
identity is attached through LiteLLM's documented `x-litellm-spend-logs-metadata` JSON header
(the deployed `tana-litellm-proxy` image, `tana/litellm_proxy/BUILD.bazel`, layered on the
`litellm_upstream` pin in `MODULE.bazel`). That version consumes the header
(`_get_spend_logs_metadata_from_request_headers`) in its common request setup used by both native
`/v1/messages` and `/v1/responses`, including their streaming paths. The authoritative metadata
object is:

```json
{
  "agentplane.namespace": "...",
  "agentplane.service_account": "...",
  "agentplane.pod_name": "...",
  "agentplane.pod_uid": "..."
}
```

`attribution` maps literal output keys to one of four verified Kubernetes principal fields:
`namespace`, `service_account`, `pod_name`, or `pod_uid`. The default sends the four
`agentplane.*` JSON members in `x-litellm-spend-logs-metadata`. Staging also configures
four `litellm.metadata.spend_logs_metadata.agentplane.*` W3C baggage members, which
LiteLLM places on its normal request span before the upstream call. For example, a custom
JSON field can be configured as:

```yaml
attribution:
  json_headers:
    - name: x-litellm-spend-logs-metadata
      fields:
        - key: agentplane.pod_name
          source: pod_name
  baggage:
    - key: litellm.metadata.spend_logs_metadata.agentplane.pod_name
      source: pod_name
```

Keys are literal, including dots and case. The ingress removes caller copies of configured
JSON headers and all caller baggage, then constructs the configured values from its
authenticated principal. It preserves trace context headers so the request stays linked
to its caller's trace.

Incoming LiteLLM metadata/customer/agent headers and Agentplane/Sandbox/Pod/Agent/Thread identity
headers are removed before this object is stamped. Request bodies remain provider-native and are
never identity evidence; a caller's body `metadata`, Agent, Thread, Pod, or Sandbox fields cannot
replace the server-stamped object.

The workload bearer is sent only to Kubernetes TokenReview. The LiteLLM virtual key is sent only on
the internal LiteLLM hop. Neither credential is logged, placed in errors, returned to callers, or
mounted into a runner or harness container.

## Per-model client configuration

The authenticated `GET /agentplane/model-config?model=...` endpoint returns a
`ModelConfig` record for an exposed route, or JSON `null` (status 200) for a route
with no configuration, on which the runner uses harness defaults. The ingress retains
complete records, indexed by their exposed model IDs, rather than reducing them to
integer budgets. Lookups are answered locally and never forwarded to LiteLLM. Any
non-200 status, including 404, fails the runner's lookup.

Configuration is authored as a list in ingress settings, for example:

```yaml
models:
  - model: ollama/olm-chat/qwen3.8-flash-next-iq4xs-128k
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

## Codex model catalog

When `codex_model_catalog_file` is set, the authenticated `GET /agentplane/codex/models` endpoint
serves that JSON file with an `ETag` that is a hash of its bytes. The file is `{"models": [...]}`
with entries in Codex's `ModelInfo` form; the ingress refuses to start on what Codex would refuse (unique
non-empty slugs, at most 1 MiB); Codex also needs instruction text in every entry, which the cdk8s test checks
by parsing the generated file with the pinned Codex. It reads the file once at startup and answers from memory, never from
LiteLLM, so a LiteLLM outage cannot make a Codex refetch fail. Without the setting the endpoint answers 404
and is not forwarded.

Every `/v1/responses` reply then carries the same value as `X-Models-Etag`. A running Codex that sees
a value different from the `ETag` it holds refetches the catalog before its next turn, so an edited
catalog (a new file, a restarted ingress) reaches running Sessions without restarting their
harnesses. The runner sets the Codex provider's `model_catalog_url` to this endpoint and passes no context window of its own. cdk8s generates the file for each environment
(`cluster/cdk8s/agentplane/codex_models.py`): a ChatGPT route takes the pinned Codex's upstream entry, another
route an entry only where a context window is configured. The pinned Codex must not be older than the runner image's
(nixpkgs' `codex`, `runner/image.nix`). Codex behavior behind this endpoint, with source citations and test
results: [`debug/codex_model_catalog.md`](../debug/codex_model_catalog.md).

For targeted debugging, set `log_llm_requests: true` in the ingress settings (or
`AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=true`). The ingress logs each request body and every raw
response chunk with a per-request ID and chunk number, plus whether the stream completed. This can
include prompts, reasoning, generated text, and tool arguments; keep it disabled by default and
restrict access and retention while enabled. Authorization and other request headers are not logged.

Each environment projects its Claude routes through the explicit runner budget map
into `LlmIngressProps.models` (Codex budgets are catalog entries, above), then the generated protobuf configuration. `configpb/config.proto` owns the lightweight
`ModelConfig` record; the HTTP endpoint keeps its existing snake_case JSON and numeric budget.
These are configured harness overrides, not a catalog of every known model capacity.

## Contract tests

The Go ingress tests exercise request filtering, TokenReview response policy, proxy errors, streaming,
and cancellation with `httptest` servers. A typed TokenReview fake is used for handler behavior;
it checks ingress interpretation and does not establish Kubernetes token-validation or Pod-binding
semantics. One smoke test starts the Bazel-built binary with YAML and environment settings against a
narrow TokenReview HTTP fixture and fake LiteLLM, covering client-go wiring and the real listener
configuration path.

```sh
bbr test //agentplane/llm_ingress/...
```

## Configuration

`AGENTPLANE_LLM_INGRESS_CONFIG_FILE` names a YAML/JSON protobuf settings file. Top-level scalar
`AGENTPLANE_LLM_INGRESS_*` environment values override it; empty environment values are ignored.
Lists, maps and attribution remain file-only. Unknown and duplicate file keys are rejected.
The Go loader owns omitted listener and attribution defaults and validates the completed config.
Python synthesis validates the complete nested model and attribution values, including header
restrictions and uniqueness, then serializes the file fragment. It does not manufacture missing
environment values or credentials to claim full validation. The LiteLLM key remains a Kubernetes
Secret reference and is never serialized into the ConfigMap; config errors omit values.

Model budgets are `int64`: canonical protobuf JSON writes them as decimal strings in YAML, while
`GET /agentplane/model-config` retains numeric JSON for the runner.

## Trusted central Session attribution

`central_sampler_accounts` is a default-empty list of authenticated `namespace/name` accounts.
Only those callers may supply `x-agentplane-session-id`, and its value must be a canonical UUID.
The ingress strips that header and generates `agentplane.session_id` in the trusted LiteLLM spend
metadata alongside the Kubernetes principal fields. A non-allowlisted caller's claim is rejected.
This is an asserted conversation identity from an explicitly trusted shared sampler; it does not
replace TokenReview or authorize arbitrary Session reads. LiteLLM promotes the configured Session
metadata into exported traces; the frontend's metadata-key setting selects the matching Langfuse
filter. Existing wrapped harness attribution continues to use its Sandbox identity.
