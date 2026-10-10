# Served-model catalog

> This guide describes landed source wiring, not verification of live rollout.
> The [model-roster and consumer-configuration design](../../model_catalog/design.md)
> proposes changes that are not enabled by merging documentation. The
> [documentation consolidation options](../../model_catalog/migration_inventory.md#documentation-consolidation)
> discuss a smaller cluster wiring/operations guide; the exact fate is not yet decided.

[`model_catalog`](../../model_catalog/README.md) owns shared model facts, named routes,
client lanes, and Nix wrapper selections. `cluster/cdk8s/model_selections.py` owns
cluster environment, picker, and harness-override selections. The catalogue has no
cdk8s dependency; Kubernetes generator internals remain visibility-restricted.

## Ownership

`model_catalog/ollama.py` owns Ollama source declarations. They feed both the
Ollama setup-script renderer and the gateway catalogue below; cdk8s retains storage,
workloads and provisioning selections. Requested `num_ctx` is not a capacity fact.

- `Model` describes a model **as served by its account**: upstream identity, known
  display name, input/output declarations, and reasoning capability. `limits` is a
  complete `TokenLimits` pair or absent, never a generic context window. See the
  [limit semantics and remaining provenance gaps](../../model_catalog/design.md#current-token-limit-shape);
  a configured Ollama context is not proof of attended capacity.
- `Upstream` associates an account with its adapter, outbound protocol. Cluster endpoints
  and credential references live separately in `litellm/upstreams.py`. The shape comes from the adapter/protocol, not a second
  independently authored value. It is not the client's inbound protocol.
- `Route` associates those values with the actual upstream model/tag, exposed identity,
  and route-specific reasoning options. Consumers read `route.id`; they do not provide
  the provider/shape/model ingredients again.
- `RouteAlias` references a canonical route. The embedding compatibility identity and
  hidden Codex recognition alias retain their existing behavior.

The same upstream slug can identify different routes and limits through different
accounts. A route's context variant and upstream tag are bound together in the catalog.
Do not join metadata using a route's trailing string segment.

Missing names fail when a route is offered in the app, rather than being synthesized
from slugs. Missing limits remain unknown. `publish_limits` preserves which routes
currently override LiteLLM's metadata; knowing a limit does not automatically authorize
changing proxy behavior or applying a harness context override. Where we do publish
limits, the LiteLLM projection emits `max_input_tokens` plus both `max_output_tokens`
and legacy `max_tokens`, with the latter two derived from one output declaration.
This uses ordinary config, not a LiteLLM patch; see [publication ownership](../../model_catalog/litellm_metadata.md#publication-ownership).
Routes without overrides still have catalogue/adapter fallback **during migration**;
the target is explicit Ducktape-owned token metadata for every served route, optionally
copied from a reviewed catalogue entry with provenance. Client budgets and embedding/audio
metadata remain unchanged. Direct Anthropic/Gemini/Mistral/Groq chat routes now publish
explicit pairs; their [source ledger](../../model_catalog/litellm_metadata.md#direct-provider-sources-2026-10-05)
distinguishes provider documentation, reviewed catalogue entries, and the Groq backup.

## Projections

| Consumer               | Input                                                               | Output                                                       |
| ---------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| LiteLLM                | `SERVED_ROUTES`, `HIDDEN_ALIASES`, deployment bindings              | Proxy config and alias settings                              |
| Terraform virtual keys | `KEY_MODEL_LANES`                                                   | `model_lanes`: allowed IDs and ordered fallback IDs          |
| Agentplane app         | `HarnessRoutes`                                                     | App-owned `ModelCatalog` records and harness ID lists        |
| OpenClaw public coder  | Explicit selections and budgets in `public_coder/app.py`            | OpenClaw IDs, names, limits, and reasoning flags             |
| Parked Haku OpenClaw   | Selected subscription routes and command aliases                    | Native Claude Code model slugs                               |
| Gatus                  | Selected Ollama route                                               | Probe request model ID                                       |
| LLM ingress            | Environment-selected routes and explicit `RUNNER_CONTEXT_OVERRIDES` | Ingress-owned model configuration and authenticated response |

For example, a preset chooses `GPT6_LUNA_RESPONSES`; the app renderer emits its ID,
display name, and reasoning choices. The key renderer emits only its ID. Neither knows
how to construct a ChatGPT Responses route name.

Authorization and picker policy are intentionally distinct. An Ollama key admits both
wires while the Agentplane picker offers only the OpenAI-compatible route. Adding a
served model does not necessarily add it to a picker or a restricted key. Defaults
remain explicit consumer choices referencing existing routes.

## Consumer boundaries

Python consumers select route objects. OpenClaw's account labels and parked Haku's
command aliases remain presentation specific to those consumers. Public Coder's
retained renderer owns explicit selections and OpenClaw `contextWindow`/`maxTokens`
settings; provider metadata no longer controls their values or whether a model is
offered. Required names/reasoning capabilities still come from the selected route.
The runner's explicit route-to-budget map remains limited to Qwen IQ4_XS. It no longer
reads the model's context metadata or derives a budget from Ollama `num_ctx`.

`model_catalog/nix.py` projects to `model_catalog/claude-wrappers.json`, read by
Nix wrappers as gateway options. It owns explicit Claude context/output settings;
omission leaves the client default, independently of the provider metadata. Regenerate
it independently of Kubernetes with
`bb run //model_catalog:generate_nix`.

Each `KEY_MODEL_LANES` entry is one `ModelLaneRoutes(allowed=..., fallbacks=...)`
record. Allowed routes scope virtual keys; ordered fallbacks configure team wildcard
routing and do not grant additional access. Empty fallbacks mean no configured
fallback. The Terraform projection emits one `model_lanes` map whose values contain
`allowed_models` and `fallback_models`, not parallel dictionaries. Lane names bind
these records to Terraform keys/teams; keys may combine several lanes. Regenerate
the Terraform CR with `bb run //cluster/cdk8s:generate_manifests`.

Claude's `[1m]` request convention stays in `litellm-claude.nix`, separate from the
served ID. The Antigravity wrapper explicitly retains its configured 65,536 output
setting, while the account metadata remains 65,535. Changing that policy requires a
separate behavioral change; do not replace account metadata with a borrowed Google API
limit.

Each environment projects its selected routes and explicit runner budgets into
`LlmIngressProps.models`. Ingress serializes those values through its
lightweight `ModelConfig` schema; the runner validates the same API contract. Ingress
retains complete records rather than flattening them to budgets. The current field,
`total_context_budget_tokens`, is a configured input-plus-output total client budget,
not independently attainable input/output maxima.
The removed `Environment.model_routes` placeholder stays removed: no app-config
strings are read back, and provider limits or GGUF context are not used as budgets.
Only an explicit no-override response retains harness defaults; authentication,
transport, malformed-response, and wrong-route errors fail the lookup.

TODO(#9574): support exposed model IDs different from LiteLLM IDs for harness
model-name recognition; coordinate inference and lookup identities as described in
[the ingress contract](../../agentplane/llm_ingress/README.md#per-model-client-configuration).
No model renaming or request/response translation is implemented here.

Runtime services own their configuration/API schemas and consume serialized data;
they do not import generator internals. Diagnostic probes and acceptance tests likewise
consume generated configuration or deployed APIs.

## Checks

Whole-tree manifest parity covers the Kubernetes consumer projections and Terraform
inputs. Focused tests check route uniqueness, alias targets, key/picker boundaries, and
unknown-metadata behavior. Nix JSON has its own artifact parity test because its generator is independent of Kubernetes. Negative
lane tests reject unserved allowances and unauthorized fallbacks. Do not duplicate
these checks with a second handwritten inventory or tests that repeat renderer field
assignments.

## Parked Agentplane Claude offerings

During the [model-roster audit](https://github.com/agentydragon/ducktape/issues/9121),
Agentplane staging and testing offer only Codex in new-session launch forms. An empty
Claude list in the app model catalogue disables that harness in those forms; a new
session defaults to an available harness. Staging's Haku preset follows: it launches
Codex against the local Qwen3.8 route's 256K window, the widest one the runner is
configured for, and the wider 128K/256K choice is otherwise an operator's per-session
pick. Both Qwen windows carry their declared reasoning efforts on the OpenAI-compatible
wire only, which is why the preset names that one.

This is an offering pause, not a runtime prohibition: existing Claude sessions can
still resume, receive commands, and show history. Explicit low-level session launches,
the native Claude adapter, credentials, and shared Anthropic/LiteLLM ingress remain.
Direct local clients and Claude Code Web are unaffected.

To restore the offerings, repopulate `STAGING_APP_MODELS.claude` and
`TESTING_APP_MODELS.claude` in `cluster/cdk8s/model_selections.py`. Restoring Haku's
Claude default is optional and separate: Haku's preset is defined in `staging_config.py`,
where its `model` takes any route, and a Thread cannot move to a model with a different
configured context window, so existing Haku Threads stay on Codex. Resolve the tracked
client-budget/metadata questions before restoring Claude; regenerate manifests and
check both launch forms. No session or volume migration is part of either pause or restoration.
