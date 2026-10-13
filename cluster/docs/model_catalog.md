# Served-model wiring and operations

This guide describes landed cluster configuration, not proof of live rollout. Shared
semantics and ownership live in the [model-catalogue design](../../model_catalog/design.md);
current work and dated deployment evidence live in [#9574](https://github.com/agentydragon/ducktape/issues/9574).

## Deployment bindings

[`model_catalog`](../../model_catalog/README.md) supplies source declarations, named
routes and lane policies without importing cdk8s. `cluster/cdk8s/model_selections.py`
owns cluster consumer selections; `cluster/cdk8s/litellm/upstreams.py` binds catalogue
upstreams to deployment endpoints and credential references. Ollama's deployment
selects source declarations from `model_catalog/ollama.py` for provisioning.

LiteLLM configuration projects `SERVED_ROUTES` and `HIDDEN_ALIASES` through those
bindings. Token publication and the transitional `publish_limits` gate follow the
[publication contract](../../model_catalog/litellm_metadata.md#publication-ownership);
provider metadata does not select client budgets.

## Projections

| Consumer                     | Input                                                               | Output                                                       |
| ---------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| LiteLLM                      | `SERVED_ROUTES`, `HIDDEN_ALIASES`, deployment bindings              | Proxy config and alias settings                              |
| Terraform virtual keys       | `KEY_MODEL_LANES`                                                   | `model_lanes`: allowed IDs and ordered fallback IDs          |
| Agentplane app               | `HarnessRoutes`                                                     | App-owned `ModelCatalog` records and harness ID lists        |
| Paused Public Coder renderer | Explicit selections and budgets in `public_coder/app.py`            | OpenClaw IDs, names, limits, and reasoning flags             |
| Parked Haku OpenClaw         | Selected subscription routes and command aliases                    | Native Claude Code model slugs                               |
| Gatus                        | Selected Ollama route                                               | Probe request model ID                                       |
| LLM ingress                  | Environment-selected routes and explicit `RUNNER_CONTEXT_OVERRIDES` | Ingress-owned model configuration and authenticated response |

The app projects named routes into IDs, display names and reasoning choices;
keys serialize route IDs. Offering a route does not grant access to it. Keep
picker/default selections separate from key policies and ordered fallbacks.
Public Coder's retained renderer owns its explicit selections and OpenClaw settings;
its pause does not remove those restoration inputs.

Nix wrapper selections and client settings belong to
[`model_catalog/nix.py`](../../model_catalog/nix.py). Its generated JSON is independent
of Kubernetes; generation instructions are in the [package entry point](../../model_catalog/README.md).

Each `KEY_MODEL_LANES` entry is one `ModelLaneRoutes(allowed=..., fallbacks=...)`
record. Allowed routes scope virtual keys; ordered fallbacks configure team wildcard
routing and do not grant additional access. Empty fallbacks mean no configured
fallback. The Terraform projection emits one `model_lanes` map whose values contain
`allowed_models` and `fallback_models`, not parallel dictionaries. Lane names bind
these records to Terraform keys/teams; keys may combine several lanes. Regenerate
the Terraform CR with `bb run //cluster/cdk8s:generate_manifests`.

Each environment projects selected routes and explicit `RUNNER_CONTEXT_OVERRIDES`
into `LlmIngressProps.models`. The runner consumes the shared `ModelConfig` contract
through authenticated ingress lookup, not a separate environment/guest-config budget
map. See the [ingress contract](../../agentplane/llm_ingress/README.md#per-model-client-configuration)
for `total_context_budget_tokens`, lookup failures and the model-ID translation TODO;
[client budgets](../../model_catalog/client_budgets.md#codex-and-agentplane) describes
native harness application and session/model-switch behavior.

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

Testing is the exception: `TESTING_APP_MODELS.claude` offers the native Qwen 128K route so the
Claude harness's acceptance cases run there.

To restore the offerings, repopulate `STAGING_APP_MODELS.claude` and
`TESTING_APP_MODELS.claude` in `cluster/cdk8s/model_selections.py`. Restoring Haku's
Claude default is optional and separate: Haku's preset is defined in `staging_config.py`,
where its `model` takes any route, and a Thread cannot move to a model with a different
configured context window, so existing Haku Threads stay on Codex. Resolve the tracked
client-budget/metadata questions before restoring Claude; regenerate manifests and
check both launch forms. No session or volume migration is part of either pause or restoration.
