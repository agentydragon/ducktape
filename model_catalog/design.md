# Model rosters and consumer configuration

Status: ownership design and implementation plan, reconciled 2026-10-10. Consumer
pauses are approved and recorded under [approved pauses](#approved-pauses-and-remaining-decisions); no shared route retirement is implied.
The active Codex path has been [audited](client_budgets.md#active-codex-path-audit-2026-10-05). The overall consumer-wiring refactor,
including limits publication and its end-to-end validation, remains incomplete.

The former implementation PR [#9034](https://github.com/agentydragon/ducktape/pull/9034)
is closed as superseded, not merged; its remaining comment cleanup landed separately
in #9577. Implementation has proceeded in smaller slices tracked below. This design
does not enable the long-context experiment or authorize unresolved file dispositions.

Current work: [#9574](https://github.com/agentydragon/ducktape/issues/9574); historical revival inventory: [#9121](https://github.com/agentydragon/ducktape/issues/9121).

The program is **non-duplicated model configuration across the neutral catalogue,
cdk8s, cluster services, Nix, and clients**. Slugs, account provenance, pretty names,
capabilities, selections and policy need clear owners and straightforward projections.
Token-limit semantics exposed one difficult part of that program; investigating Codex
capacity or enabling longer context is not the program's goal or completion criterion.
Code owns executable configuration. This document owns the cross-consumer design,
constraints and rationale; the linked limits investigations provide supporting evidence, not configuration decisions.

**Decision status:** per-file destinations and changes remain open except where an
approved decision is explicitly recorded in the [migration inventory](migration_inventory.md). The
ownership shapes and consolidation suggestions below are proposals to evaluate, not
instructions to execute. The inventory records what needs a disposition decision. Agreed goals, dependency/safety constraints and already-approved pauses remain
in force; listing a file does not authorize modifying, deleting or re-enabling it.

## Routing review checkpoint, 2026-10-10

The operator requested a [routing-contract review](routing_review.md), including
LiteLLM's transformations and possible alternatives, after finding catalogue-derived
request defaults. This review precedes **further roster restructuring**, not just token-metadata work:
the resulting architecture may remove configuration/projection responsibilities.
Revise the roster implementation plan after the routing decision.
The all-routes token-publication target below is **under reconsideration**,
not the next implementation requirement. Existing overrides and runtime policy remain
unchanged pending a decision. No router replacement or metadata removal is approved.
The primary acceptance criterion is working clients on required protocols, with usable
route context not accidentally reduced by gateway or harness fallback metadata; see
the [end-to-end requirements](routing_review.md#primary-success-criterion-working-clients-and-usable-route-context).

## Reading guide

- **This design:** [goals](#scope-and-priorities), [ownership and examples](#ownership-and-data-flow),
  [rollout](#proposed-shape-and-rollout), [pause constraints](#approved-pauses-and-remaining-decisions),
  [shared token vocabulary](#token-limit-vocabulary), and [Ollama serving constraints](#ollama-serving-configuration).
- [Migration inventory](migration_inventory.md): files and consumers requiring disposition
  decisions, approved moves, and documentation consolidation options. Listing is not approval.
- [LiteLLM metadata](litellm_metadata.md): catalogue/override behavior, publication experiments,
  and actual readers of the fields.
- [Antigravity limits](antigravity_limits.md): provider metadata, translation behavior,
  dated Google responses and bounded serving-path evidence.
- [Client budgets](client_budgets.md): Claude/OpenClaw/Codex semantics, Agentplane wiring,
  subscription-path evidence and the separate, proposed long-context experiment and costs.

The [tracking issue](https://github.com/agentydragon/ducktape/issues/9574) owns changing
PR/rollout status and the complete parked-integration inventory. Research documents retain
dated evidence; they do not independently authorize changes to limits, budgets or routes.

## Scope and priorities

### Why we paused consumers

The Nix Claude gateway wrappers (#9112), Public Coder OpenClaw (#9116), and Agentplane
Claude offerings (#9127) were paused to **reduce the active compatibility obligations
while simplifying the roster and its consumers**. Their retained code/state and revival
requirements remain tracked in #9574. These were not prerequisites for a standalone
long-context feature, permission to delete shared routes, or permanent retirements.
Keep the requested Nix renderers and Public Coder PVCs; do not spend this refactor
building compatibility machinery for paused paths before deciding what to revive.

### What success looks like for the whole program

A model/account identity, upstream slug, canonical route ID, shared display name, or
established capability is declared once, with consumers referencing that declaration.
A new route does not require reconstructing the same string/name/facts in cdk8s, Nix,
key configuration and the app. Genuine consumer policy remains explicit: serving a
route, authorizing it, offering it, selecting it by default and configuring its client
budget are not interchangeable decisions to deduplicate into one universal roster.

Small projections into existing consumer schemas are expected. Generated JSON/YAML
repeating a value is not duplicate configuration; separately maintained source values,
slug parsers, lookups that reconstruct information already in hand, and parallel maps
are the duplication to remove. The result should be less code and fewer concepts,
not a central registry followed by a growing collection of adapters and registries.

### Active paths to preserve

The operator's currently used paths are:

1. Agentplane → Codex harness → LiteLLM → Codex/ChatGPT subscription API
   (through our subscription gateway).
2. Claude Code Web → upstream provider, without our LiteLLM.
3. Local Codex and Claude Code → upstream providers directly.

Preserve these. Moving more clients behind central LiteLLM is a desired future
capability, not permission to migrate the direct clients now. Supporting every
historical model × account × wire × harness combination is **not** a requirement.
Unused wrappers and integrations may be paused or retired after confirmation.

### Desired properties

- One neutral source of route identities, account identities, names, and established
  serving-path facts. Nix and cdk8s both consume it; neither owns the other.
- Explicit consumer choices. Being served, being authorized, being offered in a
  picker, and being the default are different decisions.
- Known generative input/output limits are a justified complete pair, or absent.
  Embeddings have input-only limits, not a fake generation ceiling. Absence must
  not silently become a claim about an unrelated upstream API.
- Client compaction budgets and serving settings have explicit owners. A setting
  does not become a capacity fact merely because several clients use the number.
- Consistent advertised limits across equivalent wires for the same serving path.
  Real differences are allowed, but must be justified rather than inherited from
  adapter-name heuristics.
- Fewer active combinations, fewer lines of plumbing, and tests of actual boundary
  behavior. Prefer deleting an unused projection to building a generalized framework.
- cdk8s configuration/manifests remain Bazel-visibility restricted. Runtime services
  receive serialized configuration, not imports of deployment generators.

### Non-goals

No universal `context_window` field, universal client-budget object, duplicate model
registry, automatic equation between `num_ctx` and input/output limits, or promise
that every client interprets a field identically. No broad LiteLLM fork or middleware
framework just to preserve unused routes. Do not remove billing metadata while
fixing token-limit publication. The ingress model-config endpoint transports explicit
client policy; it does not automatically derive budgets from provider metadata.

## Ownership and data flow

**LiteLLM is not the root of the model configuration graph.** Earlier roster drafts
blurred shared model information with a LiteLLM-centered global roster. The corrected
ownership direction is that each source owns its serving declarations, and gateways
and clients compose/select from those sources. A single source of truth means one owner
per fact or decision, not one gateway-owned registry that determines every backend.

**Preferred direction (operator leaning): layered declarations outside cdk8s.**
Source ownership does not require putting serving declarations inside the deployment
generator. Ollama-specific and LiteLLM-specific declarations can both be ordinary shared
configuration, with the gateway layer depending on source declarations, not vice versa.

```text
Outside cdk8s:
  Ollama model/variant declarations ─→ LiteLLM declarations ← other source declarations
                │                              │
                │                              └─→ gateway-client selections/settings
                │                                             └─→ Nix wrapper projection
                └─→ direct-source consumer selections

cdk8s consumers:
  Ollama declarations  ─→ Ollama provisioning/deployment
  LiteLLM declarations ─→ LiteLLM configuration/deployment
  client selections    ─→ Agentplane/OpenClaw configuration
```

- **Source declarations** own model/tag identities and serving variants, without
  knowing about LiteLLM or Kubernetes. Other upstream/account declarations can also
  serve direct clients without forcing them through the gateway's naming or policy.
- **LiteLLM declarations** reference those sources and choose adapters/wires, exposed
  route identities and aliases. They may include gateway-specific policy without
  becoming the universal definition of every source or consumer.
- **cdk8s** consumes the appropriate declaration layer and adds deployment endpoints,
  credential bindings, storage/GPU/workload configuration, provisioning execution and
  environment-specific selections. Which models Ollama provisions and which LiteLLM
  exposes remain separate choices; selecting a gateway route is not a provisioning
  command.
- **Nix and other non-cdk8s consumers** can consume the relevant declaration layers
  directly and project their own settings. Nix may still use generated JSON, but does
  not need a cdk8s-owned export merely to obtain shared gateway route identities.

This supersedes the earlier candidate of housing Ollama's model declarations inside
cdk8s and importing them from LiteLLM's cdk8s code. Provider-specific does not mean
deployment-specific, and outside cdk8s does not mean one provider-agnostic mega-roster.
Do not create a framework or one new module per conceptual box merely to mirror this
diagram; existing records may suffice. Exact modules, record APIs and the division
between reusable declarations and environment-specific roster assembly remain open,
including the eventual home/scope of `SERVED_ROUTES`.

No declaration layer imports cdk8s. Runtime services consume their own serialized
configuration; cdk8s internals remain visibility-restricted. No generated-YAML parsing,
whole deployment instantiation or live-cluster discovery is implied by composition.

The following current touchpoints identify concerns to place; they do not require all
model/route instances to remain in `model_catalog/catalog.py`:

| Owner                                          | Defines                                                                                                                | Consumers / serialization boundary                                                  |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `model_catalog/catalog.py`                     | Account/model identities, upstream slugs, shared display names/capabilities, evidenced facts, named routes and aliases | Nix and cluster generators                                                          |
| `model_catalog/policies.py`                    | Allowed routes plus ordered fallbacks in one lane record                                                               | Virtual-key/team configuration; not implicit picker policy                          |
| `cluster/cdk8s/litellm/`                       | Endpoint/auth bindings and LiteLLM projection                                                                          | Generated proxy config                                                              |
| Ollama deployment and model definitions        | Server defaults, model tags/aliases, runtime serving options                                                           | Ollama; route must resolve to the intended model definition                         |
| `model_catalog/nix.py` and Nix gateway modules | Wrapper route selections and Claude-specific settings                                                                  | Generated wrapper JSON → process environment                                        |
| `cluster/cdk8s/public_coder/app.py`            | OpenClaw model selections and client budgets                                                                           | OpenClaw configuration                                                              |
| `cluster/cdk8s/model_selections.py`            | Agentplane offers/defaults and explicit runner budget selections                                                       | App catalogue, ingress and runner configuration                                     |
| Key/team renderer and deployment bindings      | Bind neutral lane policies to actual keys/teams                                                                        | Terraform allowances and ordered fallbacks; no second handwritten route list        |
| Gatus, diagnostics and acceptance clients      | Explicit probe selection and test scenarios                                                                            | Route IDs / generated artifacts / deployed APIs, not imports of synthesis internals |
| Agentplane runner adapters                     | Applying configuration in each native harness's vocabulary                                                             | Claude environment / Codex startup options                                          |
| Native harness                                 | Reserves, compaction, metadata recognition, reported usage                                                             | Actual request construction and native telemetry                                    |

Account is not manufacturer; outbound wire is not the client-facing wire. A Claude
harness can send Anthropic Messages to LiteLLM while LiteLLM sends Responses upstream.
Equivalent model-name suffixes do not establish equivalent accounts or capacities.

Use named `Route` references until serialization. Define named routes first and
assemble rosters from them. Do not recover identity through positional unpacking,
parallel maps, or parsing the last segment of a slug.

The same route may intentionally appear in several selections: that is distinct policy,
not several definitions of the route. Default labels reuse the shared display name;
a label such as “long context (experimental)” belongs to the offering, not the model.
Provider capabilities and per-request reasoning choices likewise must not be conflated.
The app and runner may need different serialized shapes; project both from the selected
route/configuration rather than reverse-engineering one output to produce another.
Native harness IDs may differ from LiteLLM route IDs through explicit ingress translation.

The table describes ownership, not a claim that every current consumer already follows
it. Before each wiring change, trace its source declarations, renderer, generated shape,
and actual reader; name the redundant source/lookup being removed and the independently
owned policy being preserved. Paused consumers need a recorded boundary and revival path,
not an exhaustive behavior matrix in the active refactor.

### Consumer-local settings pattern (tentative agreement)

The operator is **tentatively comfortable with this pattern**: a consumer selects a
canonical route and combines shared roster facts with its own settings downstream.
This is not approval of the exact helper APIs, example values, field derivations or
file locations. Per-file dispositions remain open unless separately approved in the [migration inventory](migration_inventory.md).

For example, an OpenClaw-specific configuration projection could look like:

```python
from model_catalog.catalog import GPT6_LUNA_RESPONSES, Route


def openclaw_model(
    route: Route,
    *,
    context_budget: int,
    output_budget: int,
) -> dict[str, object]:
    return {
        # Shared identity and presentation:
        "id": route.id,
        "name": route.display_name,
        # OpenClaw-specific policy, expressed in its native schema:
        "contextWindow": context_budget,
        "maxTokens": output_budget,
    }


models = [
    openclaw_model(
        GPT6_LUNA_RESPONSES,
        context_budget=128_000,
        output_budget=16_000,
    ),
]
```

**The numbers are illustrative policy choices, not recommendations, provider-capacity
claims or verified output-cap enforcement.** The route supplies shared identity/name;
the consumer supplies its own accounting/request settings. The central roster does
not need to know OpenClaw exists. The selection and its settings stay together rather
than in a model list plus parallel override dictionaries. Agent-wide compaction policy
belongs at the agent level, not forcibly attached to every model entry.

A Claude-wrapper projection could independently accept:

```python
wrapper = claude_wrapper(
    primary=GPT6_ASTRA_MESSAGES,
    haiku=GPT6_LUNA_MESSAGES,
    max_context_tokens=128_000,
    max_output_tokens=16_000,
)
```

Here `claude_wrapper` is an illustrative consumer-local helper, not a new shared API.
It would project route IDs and Claude-specific settings into the wrapper's JSON shape;
the Nix gateway renderer would apply them in Claude's environment/configuration. The
same numeric values above do not imply equivalent Claude/OpenClaw semantics. Both
consumers remain paused; illustrating their retained renderers does not re-enable them.

These settings are not necessarily **overrides of central defaults**. They may have
no corresponding central value at all. Where justified, a known provider constraint
can validate a consumer choice, or support an explicit derivation with matching
semantics. It must not silently rewrite an independently chosen compaction budget.
Unknown provider facts remain unknown rather than being filled from client settings.

Prefer a small projection per consumer over a universal client-budget object or a
framework abstracting the superficial similarity of these helpers. This is a pattern
for separating shared facts from genuine consumer policy, not another model registry.

### Ollama serving-variant pattern (tentative agreement)

The operator is also **tentatively comfortable with one Ollama serving-variant
definition feeding both provisioning and routing**, separate from provider facts and
consumer budgets. The subsequent ownership clarification is that **Ollama owns this
serving definition; LiteLLM consumes it**, not the reverse. The preferred placement is
now outside cdk8s: Ollama declarations feed both Ollama's cdk8s generator and the
LiteLLM declaration layer, which in turn feeds LiteLLM's cdk8s generator. Exact modules,
records and interfaces remain undecided; existing records may suffice instead of adding
the illustrative class below.

```python
# Ollama-owned declarations outside cdk8s (exact module/API undecided).
QWEN_256K = OllamaServingVariant(
    model=QWEN_IQ4XS,  # Shared model identity/name/capabilities.
    base_tag="qwen3.8-flash-next-iq4xs",
    tag="qwen3.8-flash-next-iq4xs-256k",
    num_ctx=262_144,
)

# LiteLLM declarations, also outside cdk8s, reference the source declaration.
QWEN_256K_OPENAI = Route(
    upstream=OLLAMA_OPENAI,
    serving_variant=QWEN_256K,
)
QWEN_256K_NATIVE = Route(
    upstream=OLLAMA_NATIVE,
    serving_variant=QWEN_256K,
)
```

This is a sketch, not an implemented API. The serving definition describes a requested
allocation, not a measured capacity. The intended boundaries are:

```text
Ollama declaration ─┬─→ Ollama cdk8s: provisioning/deployment
                    └─→ LiteLLM declarations ─→ LiteLLM cdk8s: endpoint/auth bindings
shared model facts ─────→ source/route identity, name and capabilities
client selection ──→ direct source or gateway route + consumer-owned settings
```

- **Provisioning:** obtain the base/tag/parameters from the serving definition rather
  than independently maintaining copies in shell literals. The setup script may remain
  the execution mechanism; its fate is open. Credentials, scheduling, storage/GPU
  placement and readiness remain deployment concerns.
- **Routing:** LiteLLM independently selects which Ollama models/wires to expose.
  Both illustrated routes reference the same variant; LiteLLM combines its tag with
  the selected adapter and deployment URL/auth bindings. Prefer using the baked-in
  variant on both wires **if verified**, rather than two competing ways to set context.
  Ollama's OpenAI-compatible path does not apply native `options.num_ctx` the same way;
  identical extra request bodies across wires do not establish equivalent behavior.
- **Server defaults:** a global `OLLAMA_CONTEXT_LENGTH` may still be useful, but it
  must not silently determine the meaning of an explicitly named 256K variant.
- **Publication:** neither projection assigns `max_input_tokens` or `max_output_tokens`
  from `num_ctx`. Known provider limits follow the independently justified pair-or-none
  policy; LiteLLM/Ollama discovery must not silently reintroduce unjustified limits.
- **Clients:** Agentplane, OpenClaw or another consumer selects the route and owns its
  budget. A derivation from serving configuration needs explicit justification, not an
  automatic rule that every `num_ctx` becomes the harness's window.

Acceptance must check that provisioning and both selected wire paths resolve to the
intended tag/settings, not just that their generated strings agree. No capacity claim,
variant activation/retirement, client-budget change or provisioning change is approved
by recording this pattern; the corresponding file decisions in the [migration inventory](migration_inventory.md) remain open.

### First source-declaration extraction

`model_catalog/ollama.py` now owns Ollama tags, names, the server's default
`num_ctx`, and the two Qwen serving variants. `catalog.py` projects them into
LiteLLM identities; Ollama cdk8s renders its existing setup shell from the same
source declarations. Storage/shard handling, other generation parameters and the
choice of models to provision remain local to Ollama cdk8s. This is not a generic
provisioning system or an adoption of the entire illustrative API above.

Provisioning, route identities and client budgets are unchanged. Native Ollama
routes now always send their explicitly requested `options.num_ctx`, including
128K: matching the server default is not a reason to inherit it (or a model's baked
parameters). OpenAI-compatible routes omit this ignored option and rely on their
selected tag's baked settings instead.

GPT-OSS 20B's 256K/512K/1M OpenAI-compatible exposures are now parked by explicit
operator decision: the base tag had no baked aliases to select those contexts.
Native variants remain and still request `num_ctx`; that is not proof of attended
capacity. The 128K route, provisioning and storage are unchanged. Restoration needs
correct aliases and a serving-path check; see the
[parking record](migration_inventory.md#parked-gpt-oss-20b-openai-exposures).
Requested `num_ctx` is not projected into provider limits.

## Current token-limit shape

`Model.limits` holds a generative `TokenLimits` input/output pair, input-only
`EmbeddingLimits` for embeddings, or is absent.
There is no generic model `context_window`: input allowance is not combined context
capacity, and a pair does not imply simultaneous attainability of both maxima.
LiteLLM projects the mode-appropriate limits; Nix wrappers, OpenClaw, and Agentplane own their
client budgets separately and do not read it. Ollama `num_ctx` stays independent.

The mode-specific representation does **not** finish the source audit. ChatGPT's
six retained entries still include Astra's inherited Codex window and Sol/Luna's
inherited GPT-5.6 values, not independently established provider maxima. Their
comments preserve this limitation; publication coverage does not validate them.
Antigravity text declarations now use the recorded Google response/gateway convention,
and the refreshed Claude subscription roster uses sourced gateway declarations.
Neither constitutes a capacity measurement.

Ollama GGUF context facts are now source-owned separately from requested `num_ctx`;
they do not establish generative pairs. The [migration inventory](migration_inventory.md#token-metadata-ownership-migration)
records remaining provenance and mode-specific gaps. The tracker owns current
PR/rollout status; dated research remains evidence rather than an activation claim.

## Proposed shape and rollout

Apply this to the whole roster and its projections, not just token fields. Keep the
existing small separation rather than adding a meta-configuration layer:

1. **Shared facts and identities, source-owned composition:** `Model.limits` is
   `TokenLimits(max_input_tokens, max_output_tokens) | EmbeddingLimits(max_input_tokens) | None`, alongside explicit
   `Upstream`, named `Route`, and aliases referencing routes.
   Provider input/output facts carry evidence in nearby documentation/comments.
   Unknown remains unknown. No generic model `context_window`. Shared types/facts do
   not make LiteLLM the root. Prefer source declarations → gateway declarations
   outside cdk8s, with deployment generators consuming the corresponding layers.
   Source/gateway-specific records need not pretend to be universal model metadata.
2. **Serving configuration:** endpoint/auth bindings remain deployment-local;
   self-hosted runtime options are explicit and tied to real model tags. The neutral
   roster must not import cdk8s. No automatic serving-option-to-capacity conversion.
3. **Consumer configuration:** each retained consumer selects routes and owns its
   own budget vocabulary. Claude wrapper settings stay with wrappers, OpenClaw
   settings with OpenClaw, Codex launch policy with its runner adapter/configuration.
   Values can deliberately differ; sharing a number is not a reason to share semantics.
4. **LiteLLM publication:** current and legacy token metadata may coexist, provided
   their values come from one coherent authority. When Ducktape supplies a generative
   input/output pair, project the complete set of relevant fields from it, including
   LiteLLM's legacy output alias `max_tokens`; do not leave that alias to an unrelated
   catalogue default. **Every served route must ultimately publish Ducktape-owned
   token metadata.** Catalogue values may be copied into source declarations with
   revision/entry provenance, not left as an implicit runtime authority. Keep aliases
   out of the neutral schema and remove the transitional `publish_limits` gate as
   route declarations are completed. Keep embedding/
   transcription metadata separate rather than inventing a generation output ceiling.
   Do not reintroduce our unused custom `context_window` field.
5. **Native client behavior:** configure and verify the actual harness. A clean
   `/model/info` response cannot fix Codex/Claude's independent recognition tables.

The requirement is **source consistency, not legacy-field suppression**. Ordinary
`model_info` overrides supply all three token fields for currently opted-in routes;
see [publication ownership](litellm_metadata.md#publication-ownership). No downstream
LiteLLM patch, response filter or extra proxy is needed for this slice. The earlier
omission/null experiment remains useful evidence about fallback, not justification
for stripping legacy keys. Existing configured numbers still need semantic cleanup;
consistent publication does not validate them as provider limits.

The ownership policy is settled; the remaining work is to populate and review each
route's applicable metadata. Omitting overrides still permits catalogue fallback during
this migration and is **not the intended end state**. Missing values need a specific
source or retention/pause decision, not invented limits or blanket raw-API assumptions.
A new live probe is not required for every model: reviewed catalogue snapshots are valid
sources. Do not change pricing/capabilities or internal request heuristics incidentally. Legacy metadata is also read internally; keeping it aligned
with the already-configured output limit is intentional, not response-only filtering.

### Rollout and useful validation

1. Trace the retained consumers through [ownership and data flow](#ownership-and-data-flow) and the [migration inventory](migration_inventory.md); remove duplicate declarations,
   slug/name reconstruction and redundant lookups at each projection boundary. Keep
   genuine consumer selections explicit. Approved consumer pauses are recorded in [approved pauses](#approved-pauses-and-remaining-decisions). Preserve the active subscription/Responses
   path, direct clients, and retained state; verify activation separately from merge status.
2. The [active-session audit](client_budgets.md#active-codex-path-audit-2026-10-05) records the real slug, binary and resolved budget.
   Recheck this evidence when changing harness versions or recognition strategy.
3. Validate complete ordinary token overrides through load/reload and supported
   metadata schemas. Complete Ducktape-owned declarations for the remaining routes,
   with catalogue provenance where adopted; decide individual unsupported routes'
   fates rather than retaining implicit fallback. Cover DB-backed paths if retained,
   and preserve pricing, capabilities and request settings.
4. Verify retained harness startup arguments/environment and reported window with
   a bounded request. Test model switching where budgets differ. Test Ollama alias
   effectiveness only for variants we decide to keep. No silent live deployment.
5. Regenerate artifacts and check route/name propagation, key-versus-picker policy,
   fallback ordering and unknown-metadata behavior at their real boundaries. Confirm
   Nix remains independent of cdk8s and runtime code cannot import generator internals.
   Review net source/plumbing removal. Verify ingress lookup runtime acceptance
   separately from configuration rollout.

The optional long-context offering is a separate follow-up. Neither enabling it nor
finding a subscription backend maximum is a prerequisite for this wiring cleanup.

Do not build a test matrix for every retired combination. A real config-loader/API
contract test pays rent; assertions that merely repeat every roster constant do not.
Keep the overall refactor net-negative in code/plumbing where possible.

## Approved pauses and remaining decisions

Approved pause scope as of 2026-10-05. A merge is not proof of machine activation or Flux
rollout. The [tracking issue](https://github.com/agentydragon/ducktape/issues/9574)
maintains deployment status, the full parked inventory, and restoration requirements.

| Integration                      | Approved scope                                                                                   | Retained for restoration                                                                                                                        |
| -------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Five Nix Claude gateway wrappers | #9112: remove workstation activation/advertising; direct local clients unchanged                 | Nix renderers, JSON generator, credential declarations; machine activation not verified here                                                    |
| Public Coder OpenClaw            | #9116: stop OpenClaw/proxies and halt devbox                                                     | Workload definitions, namespace, PVCs and backups; live pause not reverified in this audit                                                      |
| Agentplane Claude                | #9127: empty staging/testing Claude offerings, disabled picker options, omit Haku Claude presets | Native adapter, existing sessions/resume/history, explicit low-level launches, credentials and shared ingress/routes; not a runtime prohibition |

These are consumer pauses, **not permission to remove shared GPT-through-Messages or
other served routes**. Haku OpenClaw remains parked. Do not treat its historical
intentionally deleted PVCs as permission to delete Public Coder or Agentplane state.
Keep the paused Nix rendering code in place as requested, rather than deleting it
just because Git could restore it.

The remaining simplification question is **which experimental backends must remain
actively usable now**: Ollama Qwen 128K/256K, other Ollama variants, direct Google,
Antigravity, Tana, Mistral and Groq. No blanket removal is approved. Identify key lanes,
fallbacks, aliases, monitoring and embedding dependents before proposing withdrawal.
An experiment-key grant is not a maintenance commitment for every route it admits.

Publication policy and any Codex recognition/budget change remain separate decisions.
The approved pauses reduce the immediate compatibility matrix; they do not resolve
LiteLLM's fallback metadata or establish new provider limits.

## Token-limit vocabulary

| Concept                   | Meaning                                                                         | Does not establish                                   |
| ------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Provider input ceiling    | Input accepted by this account's serving path, under documented conditions      | Combined input/output capacity                       |
| Provider output ceiling   | Output ceiling on that path; treatment of reasoning tokens is provider-specific | How much output remains after a particular prompt    |
| Combined context capacity | Tokens jointly retained/attended, with backend-specific accounting              | An independent maximum input and maximum output pair |
| Client context budget     | Client assumption used for accounting, reserves, and compaction                 | Backend capacity or successful long-context quality  |
| Request output budget     | Requested generation limit, if the serving path supports it                     | Model capability metadata or guaranteed enforcement  |
| Ollama `num_ctx`          | Requested runtime context allocation                                            | Proven attended capacity or an output ceiling        |
| Ollama `num_predict`      | Generation-length option                                                        | Context allocation                                   |
| Reported harness window   | Harness's resolved view of its context budget                                   | Measurement of what the backend actually attended    |

Even a justified input/output pair need not mean that both maxima can be attained
simultaneously. Record any combined constraint in the evidence; do not invent
`combined = input + output`, or derive input by subtracting a client output setting.
If an active path needs a combined constraint enforced, design that explicitly.

`model_info.max_tokens` is legacy **metadata**. Request-body `max_tokens` is a
**generation setting**. Projecting the former must not set or remove the latter.

## Ollama serving configuration

The inspected live/source version was **0.34.4**. Our deployment sets
`OLLAMA_CONTEXT_LENGTH=131072`; the Qwen 256K model alias bakes in `num_ctx=262144`.
See the [Ollama deployment notes](../cluster/cdk8s/ollama/README.md).

- `num_ctx` requests runtime context allocation. On the inspected GGUF path, the
  scheduler can clamp it to recorded training context. Allocation, prompt handling,
  and model quality are different evidence.
- `num_predict` controls generation length. The OpenAI-compatible `max_tokens`
  request field maps to it. It is not LiteLLM's `model_info.max_tokens`.
- Native Ollama requests can carry `options.num_ctx`. The OpenAI-compatible wire
  ignores that native option; a model alias/definition must select the context there.
- Server defaults, model-definition options, and request options are separate
  precedence layers. Inspect the effective loaded runner, not just our route name.
- Truncation/context shifting can allow successful requests without retaining all
  input. A short tool call or accepted long prompt does not prove attended capacity.

Do not auto-project `Route.num_ctx` into provider limits or every harness's budget.
Audit whether each retained variant changes actual serving behavior. In particular,
other GPT-OSS variants must not be considered validated just because Qwen's 256K
alias is wired. Pausing unused variants avoids preserving a misleading matrix.
