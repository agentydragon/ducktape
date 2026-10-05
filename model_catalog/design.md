# Model rosters and consumer configuration

Status: design proposal and investigation record, updated 2026-10-05. Consumer
pauses are approved and recorded in §5; no shared route retirement is implied.
The active Codex path has been audited below. The overall consumer-wiring refactor,
including limits publication and its end-to-end validation, remains incomplete.

Configuration changes remain separately proposed in
[#9034](https://github.com/agentydragon/ducktape/pull/9034). Merging this document
records the design and findings; it does not enable those changes or the long-context
experiment.

Tracking and revival inventory: [#9121](https://github.com/agentydragon/ducktape/issues/9121).

The program is **non-duplicated model configuration across the neutral catalogue,
cdk8s, cluster services, Nix, and clients**. Slugs, account provenance, pretty names,
capabilities, selections and policy need clear owners and straightforward projections.
Token-limit semantics exposed one difficult part of that program; investigating Codex
capacity or enabling longer context is not the program's goal or completion criterion.
Code owns executable configuration. This document owns the cross-consumer design,
constraints and rationale; the detailed limits research is supporting evidence below.

**Decision status:** per-file destinations and changes remain open except where an
approved decision is explicitly recorded in §3. The
ownership shapes and consolidation suggestions below are proposals to evaluate, not
instructions to execute. The file inventory in §3 records what needs a disposition
decision. Agreed goals, dependency/safety constraints and already-approved pauses remain
in force; listing a file does not authorize modifying, deleting or re-enabling it.

## Contents

### Core design

- [Scope and priorities](#1-scope-and-priorities)
- [Ownership and data flow](#2-ownership-and-data-flow)
  - [Consumer-local settings pattern](#consumer-local-settings-pattern-tentative-agreement)
  - [Ollama serving-variant pattern](#ollama-serving-variant-pattern-tentative-agreement)
- [Consumer inventory and side effects](#3-consumer-inventory-and-side-effects)
  - [Files requiring disposition decisions](#files-requiring-disposition-decisions)
- [Proposed shape and rollout](#4-proposed-shape-and-rollout)
- [Approved pauses and remaining decisions](#5-approved-pauses-and-remaining-decisions)
- [Documentation consolidation](#6-documentation-consolidation)

### Supporting limits research

- [Token-limit vocabulary](#appendix-a-token-limit-vocabulary)
- [LiteLLM catalogue and publication](#appendix-b-litellm-catalogue-and-publication)
- [Client-specific budgets and investigations](#appendix-c-client-specific-budgets-and-investigations)
  - [Active Codex audit](#active-codex-path-audit-2026-10-05)
  - [Subscription-path public evidence](#subscription-path-public-evidence-2026-10-05)
  - [Optional long-context experiment and costs](#opt-in-long-context-experiment-and-costs)
- [Ollama serving configuration](#appendix-d-ollama-serving-configuration)

## 1. Scope and priorities

### Why we paused consumers

The Nix Claude gateway wrappers (#9112), Public Coder OpenClaw (#9116), and Agentplane
Claude offerings (#9127) were paused to **reduce the active compatibility obligations
while simplifying the roster and its consumers**. Their retained code/state and revival
requirements remain tracked in #9121. These were not prerequisites for a standalone
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
- Known input/output limits are a justified complete pair, or absent. Absence must
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
fixing token-limit publication. Keep independent refactors out of #8899, whose scope
is the runner context-size endpoint.

## 2. Ownership and data flow

The intended configuration flow is:

```text
neutral named models/accounts/routes + explicit shared lane policies
  ├─ cdk8s deployment bindings + consumer selections
  │    ├─ LiteLLM served routes and aliases
  │    ├─ Terraform key allowances and ordered fallbacks
  │    ├─ Agentplane offerings → ingress/runner configuration → native client
  │    └─ OpenClaw/probe configuration (paused consumers remain marked as such)
  └─ Nix wrapper selections → generated JSON → wrapper environment (paused)
```

Only the common facts are shared; deployment endpoints/auth stay in cdk8s, and
client-specific settings stay with the consumer. Runtime services read their own
serialized configuration, never import cdk8s. The neutral package must remain usable
without Kubernetes, and Bazel visibility must enforce those boundaries.

| Owner                                          | Defines                                                                                                                | Consumers / serialization boundary                                                  |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `model_catalog/catalog.py`                     | Account/model identities, upstream slugs, shared display names/capabilities, evidenced facts, named routes and aliases | Nix and cluster generators                                                          |
| `model_catalog/policies.py`                    | Allowed routes plus ordered fallbacks in one lane record                                                               | Virtual-key/team configuration; not implicit picker policy                          |
| `cluster/cdk8s/litellm/`                       | Endpoint/auth bindings and LiteLLM projection                                                                          | Generated proxy config                                                              |
| Ollama deployment and model definitions        | Server defaults, model tags/aliases, runtime serving options                                                           | Ollama; route must resolve to the intended model definition                         |
| `model_catalog/nix.py` and Nix gateway modules | Wrapper route selections and Claude-specific settings                                                                  | Generated wrapper JSON → process environment                                        |
| `cluster/cdk8s/public_coder_agent_config.py`   | OpenClaw model selections and client budgets                                                                           | OpenClaw configuration                                                              |
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
file locations. Per-file dispositions remain open unless separately approved in §3.

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
consumer budgets. Neutral means independent of cdk8s/deployment, not ignorant of provider
protocols. Exact records, file locations and how provisioning consumes them remain
undecided; existing records may suffice instead of adding the illustrative class below.

```python
QWEN_256K = OllamaServingVariant(
    model=QWEN_IQ4XS,  # Shared model identity/name/capabilities.
    base_tag="qwen3.8-flash-next-iq4xs",
    tag="qwen3.8-flash-next-iq4xs-256k",
    num_ctx=262_144,
)

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
shared model facts ─────────────────────→ route identity/name/capabilities
Ollama serving variant ─┬─→ provisioning: base model → tag + parameters
                       └─→ canonical routes → LiteLLM adapter + upstream tag
                                                  ↑
                                      deployment endpoint/auth bindings
consumer selection ──→ chosen route + consumer-owned settings
```

- **Provisioning:** obtain the base/tag/parameters from the serving definition rather
  than independently maintaining copies in shell literals. The setup script may remain
  the execution mechanism; its fate is open. Credentials, scheduling, storage/GPU
  placement and readiness remain deployment concerns.
- **Routing:** both routes reference the same variant; LiteLLM combines its tag with
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
by recording this pattern; the corresponding file decisions in §3 remain open.

## 3. Consumer inventory and side effects

Before deletion or renaming, check these as well as the obvious clients:

- Terraform virtual-key allowlists, team fallback order, and experiment keys.
- Agentplane picker/default selections, LLM ingress metadata/auth routing, and
  runner environment/guest configuration. These are not interchangeable registries.
- Hidden Codex recognition aliases and the public embedding compatibility alias.
- OpenClaw durable memory: stored embedding identities can outlive a running agent.
  Pausing the app is not permission to delete/rebuild its index or change its model.
- Parked Haku OpenClaw's native CLI model aliases, Gatus's selected Ollama probe,
  inference/probe scripts, and live harness acceptance cases.
- Nix wrapper JSON, binaries, imports, and credential provisioning. Moving a source
  file to `x/` alone does not stop deploying its generated outputs.
- LiteLLM pricing/budget accounting and telemetry labels. Route cleanup must not
  accidentally bypass spending limits or relabel unrelated account costs.

This is a starting inventory, not a claim of exhaustive runtime reachability.
Repository references and operator confirmation are both needed.

### Files requiring disposition decisions

**Dispositions are OPEN unless explicitly marked approved below.** This is a review
inventory, not an approved
keep/move/merge/delete list or a promise that every file needs a diff. Paths are the
current locations; grouped paths do not imply that their fates must be the same.
For each, trace its declarations, inputs, outputs and actual readers; then record the
chosen disposition and rationale when agreed. Unchanged, simplified, moved, consolidated
or removed are possible outcomes, subject to the existing constraints. The previously
suggested fates are candidates, not commitments. Extend the inventory if the audit
finds another consumer or source of independently maintained configuration.

#### Neutral definitions and policy

| Current file                | Question to resolve                                                                                                                                                                  |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `model_catalog/catalog.py`  | Which declarations are neutral model/account/route facts, which are serving or consumer settings, and what shape eliminates duplicate identities/names without conflating semantics? |
| `model_catalog/policies.py` | Which lane/allowance/fallback choices are genuinely shared, and which belong to an individual consumer?                                                                              |
| `model_catalog/BUILD.bazel` | What dependency and visibility changes follow from the chosen ownership, while keeping Nix and runtime consumers independent of cdk8s internals?                                     |

#### LiteLLM, keys and provider adapter

| Current file(s)                                                                                                            | Question to resolve                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/litellm/config.py`                                                                                          | Which logic is necessary LiteLLM serialization, which repeats catalogue facts, and where can the agreed publication contract actually be enforced?                                                    |
| `cluster/cdk8s/litellm/upstreams.py`                                                                                       | What deployment endpoint/auth binding shape is needed without duplicating account/adapter identity?                                                                                                   |
| `cluster/cdk8s/litellm/keys.py`; `tf/gitops/litellm-keys/main.tf`                                                          | Where should lane-to-key/team binding and ordered fallback projection live, and are either side's inputs redundant?                                                                                   |
| `tana/litellm_proxy/` (including `custom_handler.py`, `provider.py`, `BUILD.bazel`, `requirements.in`, `requirements.txt`) | Does the chosen publication/adapter solution require changes here at all? Avoid an unneeded proxy framework or incidental dependency upgrade.                                                         |
| `tana/litellm_proxy/model_registry.py`                                                                                     | Which entries are necessary Tana protocol/discovery mappings versus duplicate definitions of our selected models/routes? Do not assume that another “registry” is redundant without tracing its role. |

#### Agentplane selection, projection and runtime

| Current file(s)                                                                                              | Question to resolve                                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/model_selections.py`                                                                          | Which selections belong together? Decide the homes and shapes of harness offerings, defaults, `PUBLIC_CODER_MODELS` and `RUNNER_CONTEXT_OVERRIDES` rather than preserving or moving them by assumption. |
| `cluster/cdk8s/agentplane/app_settings.py`                                                                   | What is the smallest projection into app offerings/presets, and which presentation/default choices are independently owned?                                                                             |
| `cluster/cdk8s/agentplane/environment.py`; `staging.py`, `testing.py`, `staging_config.py` in that directory | Which structured selections should flow to each renderer, and where should environment-specific choices live?                                                                                           |
| `cluster/cdk8s/agentplane/app.py`                                                                            | How should runner configuration be emitted without treating `route.model.context_window` as a universal client budget?                                                                                  |
| `agentplane/app/api.py`                                                                                      | Does the runtime-owned offering schema need to change, or can existing records express the chosen design? No generator imports.                                                                         |
| `agentplane/runner/config.py`, `main.py`, `guest_config.py`, `session.py`                                    | What runner-owned configuration shape supports launch, switching and resume without duplicate metadata or misleading shared semantics?                                                                  |
| `agentplane/runner/codex.py`; `agentplane/runner/claude.py`                                                  | Which settings must be applied in each native client's vocabulary, and what logic is unnecessary? Preserve the approved Claude offering-pause boundary.                                                 |
| `agentplane/llm_ingress/app.py`, `settings.py`; `cluster/cdk8s/agentplane/llm_ingress.py`                    | Is model-ID translation useful enough to introduce, and where would its authorized mapping/configuration belong? Pass-through is not required, but translation is not yet selected.                     |

#### Nix wrappers and direct local clients

| Current file(s)                                                                                                                          | Question to resolve                                                                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model_catalog/nix.py`                                                                                                                   | What wrapper selections/settings should it own, and what should it merely project? Its exact future location/shape is undecided.                                                          |
| `model_catalog/claude-wrappers.json`                                                                                                     | Is this generated interface still the simplest boundary, and what fields should it contain if retained? It is not a hand-maintained roster.                                               |
| `nix/home/claude_code/gateway.nix`                                                                                                       | What common wrapper rendering belongs here, and what configuration is duplicated elsewhere?                                                                                               |
| `codex-claude.nix`, `gemini-claude.nix`, `antigravity-claude.nix`, `litellm-claude.nix`, `tana-claude.nix` under `nix/home/claude_code/` | What are the eventual module boundaries and inputs? The already-approved pause and requirement to retain renderers remain; this inventory is not permission to delete or reactivate them. |
| `nix/home/codex/default.nix`; `nix/home/claude_code/default.nix`; machine activation/import configuration such as `nix/home/hosts/*.nix` | Are changes needed at all? Preserve direct-provider clients and the wrapper pause; central-gateway migration is not implicit.                                                             |

#### Ollama serving variants

| Current file(s)                                                                                         | Question to resolve                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/ollama/app.py`                                                                           | Where should server defaults and serving-variant configuration be declared, and what should deployment rendering consume?                                                                                                 |
| `cluster/cdk8s/ollama/setup-gpt-oss-v2.sh`                                                              | How should model/alias creation obtain tags and parameters without independent hard-coded copies? Decide whether this script/interface remains appropriate.                                                               |
| Ollama definitions in `model_catalog/catalog.py`; their projection in `cluster/cdk8s/litellm/config.py` | Decide jointly with the two files above: who defines variant identity, upstream tag and requested context, and how each wire actually applies them? Do not infer provider limits or client budgets from serving settings. |

#### Public Coder and smaller consumers

| Current file(s)                                                                                                                                                                                                                 | Question to resolve                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/public_coder_agent_config.py`                                                                                                                                                                                    | Where should OpenClaw selections, labels and client budgets live? What retained renderer is useful for revival without supporting every paused combination now?      |
| Public Coder workload/storage configuration, including `cluster/cdk8s/public_coder_devbox.py`, `public_coder_proxy.py`, `public_coder_egress.py`, `public_coder_sshpiper.py`, `public_coder_backup.py` and associated manifests | Which files actually depend on roster decisions? No change may incidentally undo the pause, delete retained storage/backups, or change a durable embedding identity. |
| `cluster/cdk8s/parked/haku_openclaw_spike_config.py`                                                                                                                                                                            | What dependencies and revival information need to remain documented, and is any source change needed while parked?                                                   |
| `cluster/cdk8s/gatus/config.py`                                                                                                                                                                                                 | Is probe selection already a sufficient projection of canonical routes, or does it duplicate naming/selection logic?                                                 |

#### Generated outputs, tests, build boundaries and documentation

| Current file(s)                                                                                                                                                                               | Question to resolve                                                                                                                                                                      |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Affected `cluster/k8s/…` manifests and Nix JSON artifacts                                                                                                                                     | Which outputs follow from the eventual source changes? Regenerate them from their owners, never make them another authored source.                                                       |
| `cluster/cdk8s/test_model_rosters.py`; `cluster/cdk8s/litellm/test_config.py`, `test_openclaw_models.py`; `model_catalog/test_nix.py`, `test_policies.py`; affected Agentplane/OpenClaw tests | Which tests prove identity, authorization, fallback, serialization or native-client behavior, and which merely restate fields? Decide retention/consolidation based on that distinction. |
| Affected `BUILD.bazel` files, including `cluster/cdk8s/BUILD.bazel`, `cluster/cdk8s/litellm/BUILD.bazel`, `cluster/cdk8s/agentplane/BUILD.bazel`                                              | Which dependency/visibility edges should change as ownership is decided? Preserve the cdk8s generation/runtime boundary.                                                                 |
| `model_catalog/design.md`; `model_catalog/README.md`; `cluster/docs/model_catalog.md`                                                                                                         | Decide their eventual content split and lifecycle without competing specifications or lost evidence/restoration instructions. §6 offers one candidate, not an approved per-file outcome. |

### Approved file disposition: cross-layer harness audit

**Approved by the operator; carried out in this documentation PR:** move
`agentplane/docs/model_metadata.md` to
[`model_catalog/debug/harness_model_metadata.md`](debug/harness_model_metadata.md).
Its LiteLLM naming/authorization, Nix-wrapper and native-client findings span consumers;
Agentplane supplied the capture machinery but does not own all those findings. Preserve
the historical versions and evidence, update relative links, and link from Agentplane's
capture documentation rather than keeping a duplicate or redirect stub. Capture tools
and runtime code stay in Agentplane. This decision does not settle any other file's fate.

## 4. Proposed shape and rollout

Apply this to the whole roster and its projections, not just token fields. Keep the
existing small separation rather than adding a meta-configuration layer:

1. **Neutral facts and identities:** `Model(..., limits=TokenLimits(input, output)
| None)`, explicit `Upstream`, named `Route`, and aliases referencing routes.
   Provider input/output facts carry evidence in nearby documentation/comments.
   Unknown remains unknown. No generic model `context_window`.
2. **Serving configuration:** endpoint/auth bindings remain deployment-local;
   self-hosted runtime options are explicit and tied to real model tags. The neutral
   roster must not import cdk8s. No automatic serving-option-to-capacity conversion.
3. **Consumer configuration:** each retained consumer selects routes and owns its
   own budget vocabulary. Claude wrapper settings stay with wrappers, OpenClaw
   settings with OpenClaw, Codex launch policy with its runner adapter/configuration.
   Values can deliberately differ; sharing a number is not a reason to share semantics.
4. **LiteLLM publication:** implement one narrow, tested policy at the proxy boundary,
   only after selecting the supported paths. For generative models, publish our
   established input/output pair together or neither; do not publish the legacy
   `max_tokens` or custom `context_window` as independent competing capacity facts.
   Explicitly separate embedding/transcription metadata rather than inventing an
   output ceiling to satisfy a generation-only pair rule.
5. **Native client behavior:** configure and verify the actual harness. A clean
   `/model/info` response cannot fix Codex/Claude's independent recognition tables.

Item 4 is the desired contract, **not a solved implementation**. First test whether
an upstream-supported configuration mechanism can satisfy it through registration,
reload, list and single-deployment endpoints. If not, compare a small upstream fix
or targeted response projection against leaving LiteLLM's endpoint explicitly
non-authoritative. The latter does **not** satisfy the requested public pair-or-none
contract and requires an explicit decision, not silent acceptance. Avoid global
model-cost mutation or a broad wrapper service as a premature solution.

Internal catalogue use also needs an explicit decision: suppressing fields in an
HTTP response does not suppress LiteLLM's request-side limit heuristics. Determine
which checks execute on the retained path before changing them; preserve accounting.

### Rollout and useful validation

1. Trace the retained consumers through §2 and §3; remove duplicate declarations,
   slug/name reconstruction and redundant lookups at each projection boundary. Keep
   genuine consumer selections explicit. Approved consumer pauses are recorded in §5. Preserve the active subscription/Responses
   path, direct clients, and retained state; verify activation separately from merge status.
2. The active-session audit in Appendix C records the real slug, binary and resolved budget.
   Recheck this evidence when changing harness versions or recognition strategy.
3. Finish the narrow LiteLLM publication experiment using the proxy-local pin, both
   bundled and controlled remote catalogue fixtures. Check load/reload and both
   metadata endpoint shapes, with a known pair, unknown limits, and legacy fallback.
4. Verify retained harness startup arguments/environment and reported window with
   a bounded request. Test model switching where budgets differ. Test Ollama alias
   effectiveness only for variants we decide to keep. No silent live deployment.
5. Regenerate artifacts and check route/name propagation, key-versus-picker policy,
   fallback ordering and unknown-metadata behavior at their real boundaries. Confirm
   Nix remains independent of cdk8s and runtime code cannot import generator internals.
   Review net source/plumbing removal, then adapt #8899 separately.

The optional long-context offering is a separate follow-up. Neither enabling it nor
finding a subscription backend maximum is a prerequisite for this wiring cleanup.

Do not build a test matrix for every retired combination. A real config-loader/API
contract test pays rent; assertions that merely repeat every roster constant do not.
Keep the overall refactor net-negative in code/plumbing where possible.

## 5. Approved pauses and remaining decisions

Source status checked 2026-10-05. A merge is not proof of machine activation or Flux
rollout. The [tracking issue](https://github.com/agentydragon/ducktape/issues/9121)
maintains deployment status, the full parked inventory, and restoration requirements.

| Integration                      | Approved scope and source status                                                                                                 | Retained for restoration                                                                                                                        |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Five Nix Claude gateway wrappers | **#9112 merged**: remove workstation activation/advertising; direct local clients unchanged                                      | Nix renderers, JSON generator, credential declarations; machine activation not verified here                                                    |
| Public Coder OpenClaw            | **#9116 merged**: stop OpenClaw/proxies and halt devbox                                                                          | Workload definitions, namespace, PVCs and backups; live pause not reverified in this audit                                                      |
| Agentplane Claude                | **#9127 merged**, exact-head CI green: empty staging/testing Claude offerings, disabled picker options, omit Haku Claude presets | Native adapter, existing sessions/resume/history, explicit low-level launches, credentials and shared ingress/routes; not a runtime prohibition |

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

## 6. Documentation consolidation

[`cluster/docs/model_catalog.md`](../cluster/docs/model_catalog.md) is part of the
refactor, not a second specification to leave untouched beside this design. **One
candidate is to retain it as a smaller cluster wiring and operations guide; its exact
fate and the content split below remain undecided.** Do not move the neutral
catalogue's ownership back under `cluster/`, or maintain parallel explanations of
model-limit semantics in both places.

| Document                                        | Candidate responsibility (not yet agreed)                                                                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| This design                                     | Cross-layer semantics, constraints, decisions and unresolved choices; explicitly dated investigation evidence                                                   |
| `model_catalog/README.md`                       | Short neutral-package entry point: module responsibilities, generation entry points, links to the design and deployment guide                                   |
| `cluster/docs/model_catalog.md`                 | Current cluster bindings and projections, where to change deployment selections, regeneration/check commands, and cluster-specific pause/restoration procedures |
| `model_catalog/debug/harness_model_metadata.md` | **Location approved (§3):** historical version-scoped cross-layer audit, linked as evidence rather than treated as current deployment policy                    |
| Tracking issue #9121                            | Work/PR status and complete parked-integration inventory, linking to the relevant restoration instructions                                                      |

### Candidate treatment of the existing cluster guide

The following suggestions apply if we choose the smaller-guide option; they are not
a commitment to retain these sections or this exact document structure.

- **Ownership:** replace the duplicated `Model`/`Upstream`/`Route` definitions and
  generic context-limit explanations with links to the neutral package and this
  design. Keep cluster endpoint/credential binding locations and the visibility and
  serialized-runtime boundaries.
- **Projections:** retain a compact cluster-consumer wiring table, grounded in the
  landed code. Clearly distinguish active consumers from paused renderers. Do not
  maintain another hand-written roster, token-limit table, or route-construction rule.
- **Consumer boundaries:** retain cluster authorization versus offering/default policy
  and the actual runner/ingress configuration flow. Replace Nix-wrapper budget details
  with links to their owning documentation/code. Replace speculative ingress follow-up
  prose with actual wiring when that change lands, including any model-ID translation.
- **Checks and regeneration:** keep the practical manifest/key generation and validation
  entry points; link to the neutral Nix generator rather than duplicating its contract.
- **Parked Agentplane Claude:** retain the deployment-specific offering-pause semantics
  and restoration steps already here. Do not move them into a generic Agentplane
  service document or lose them while consolidating. Link the tracking issue for the
  full parked inventory; this guide need not duplicate its PR/rollout history.

### Update discipline

The cluster guide describes **landed source configuration**, not proof of live rollout.
The docs-only PR does not remove `publish_limits`, `PUBLIC_CODER_MODELS`, or any other
currently implemented behavior. Each implementation PR must update the affected guide
sections alongside its code, removing obsolete names and claims rather than appending
another migration note. Remove the temporary proposal notice when no longer useful.

As decisions land, mark them implemented here and remove resolved alternatives and
completed rollout checklists; retain only useful dated evidence/rationale. Do not copy
this entire design into the cluster guide, create a third overview, or keep two current
specifications. Whichever document split is chosen, completion means the documentation
is accurate and non-duplicated, neutral semantics have one home, and paused integrations
remain recoverable.

## Appendix A. Token-limit vocabulary

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
**generation setting**. Removing the former must not remove the latter.

## Appendix B. LiteLLM catalogue and publication

### Evidence scope

The proxy's package-local lock and upstream image pin are LiteLLM **1.100.1**.
The root Python lock is not the proxy runtime. Relevant upstream source files are
`utils.py`, `router.py`, `proxy/proxy_server.py`,
`litellm_core_utils/get_model_cost_map.py`, and `llms/ollama/common_utils.py`.
The wheel's `model_prices_and_context_window_backup.json` and the
[remote catalogue](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
were inspected separately. The remote snapshot below was fetched 2026-10-05.

Unless configured otherwise, LiteLLM downloads the remote catalogue; the bundled
file is a fallback. `LITELLM_LOCAL_MODEL_COST_MAP=True` selects the bundled map.
Pinning the package alone does **not** pin the catalogue. Neither source is evidence
for our subscription gateway's limits.

### What happens to metadata

1. LiteLLM resolves `litellm_params.model`, including its adapter prefix, against
   catalogue candidates with provider-compatibility checks.
2. Exact misses can use catalogue regex generalizations. Native Ollama may query
   `/api/show`; there is more than a static JSON lookup involved.
3. Deployment `model_info` registers/merges overrides into LiteLLM's model-cost map.
   This map contains both capacity/capability and pricing metadata.
4. Router objects and metadata endpoints perform further serialization and merging.
   List and single-deployment `/model/info` paths are not identical.

Omitting a key permits fallback. Explicit YAML `null` is **not a reliable deletion
operator**: `register_model()` uses `_update_dictionary()`, which ignores `None`;
router serialization uses `exclude_none=True`; the single-deployment metadata
endpoint removes nulls before filling missing keys. The final response merge alone
is therefore insufficient evidence that null suppression works.

This is a source-traced finding, not a completed end-to-end null-config test. That
experiment remains a prerequisite for any chosen publication implementation.

### Token KVPs in the remote catalogue

These are catalogue defaults, **not our serving-path facts**. Values are tokens.
Other catalogue KVPs include mode, pricing, caching rates, and capability flags;
those need separate treatment, not deletion alongside limits.

| Matching model family / adapter                               | `max_input_tokens` | `max_output_tokens` | `max_tokens` |
| ------------------------------------------------------------- | -----------------: | ------------------: | -----------: |
| OpenAI GPT-6 Astra/Sol/Luna; GPT-5.6 Sol/Terra/Luna           |             922000 |              128000 |       128000 |
| OpenAI GPT-5.4 / GPT-5.5                                      |            1050000 |              128000 |       128000 |
| Anthropic Opus/Sonnet/Fable 5; Sonnet 4.6                     |            1000000 |              128000 |       128000 |
| Anthropic Haiku 4.5                                           |             200000 |               64000 |        64000 |
| Google Gemini 3.7 Flash / 3.5 Flash Lite                      |            1048576 |               65536 |        65536 |
| Google Gemini Embedding 2                                     |               8192 |              absent |         8192 |
| Google Gemini Embedding 001                                   |               2048 |              absent |         2048 |
| Mistral Codestral / code / code-FIM                           |             128000 |              128000 |       128000 |
| Mistral Magistral, Ministral 8B/14B, medium/small/vibe routes |             262144 |              262144 |       262144 |
| Mistral Ministral 3B                                          |             131072 |              131072 |       131072 |
| Mistral Voxtral Small                                         |              32768 |               32768 |        32768 |
| Groq Whisper                                                  |             absent |              absent |       absent |

The bundled catalogue differs: GPT-6 entries are absent; Magistral has all three
values at 40000; `mistral-medium` has input 32000, output/legacy 8191. Bundled Groq
Llama 3.3 70B has 131072/32768/32768, and Llama 3.1 8B has all three at 131072;
those exact entries are absent from the remote snapshot.

Additional cases:

- `openai/gpt-*` can use OpenAI entries. `anthropic/gpt-*` fails that provider match.
- Antigravity `anthropic/gemini-*` does not simply inherit Google entries; its
  `anthropic/claude-sonnet-4-6` can inherit Anthropic's raw-API entry.
- A Claude-family regex supplies input 200000, output 64000, legacy 64000 even
  without an exact entry. It matches our Tana Claude names and the Antigravity
  `claude-opus-4-6-thinking` name.
- Our exact local Ollama tags lack static entries. Native discovery can read recorded
  `context_length` from `/api/show` and put it into **all three** token-limit fields.
  That is not a measured output ceiling or necessarily the effective `num_ctx`.
- `context_window` is not a standard catalogue field used by this projection. Our
  former custom field should not be smuggled back in as a generic client budget.

### Observed deployed mixture

On 2026-10-05, `/model/info` under the cheap-experiments key returned:

| Luna route                         |  Input | Output | Legacy |
| ---------------------------------- | -----: | -----: | -----: |
| `chatgpt/ant-messages/gpt-6-luna`  | 372000 | 128000 |   null |
| `chatgpt/oai-responses/gpt-6-luna` | 372000 | 128000 | 128000 |

These responses contain existing overrides. They are not pristine catalogue
lookups, and the restricted key does not establish the state of every deployment.

### Who consumes these KVPs?

- `/model/info` clients and the LiteLLM dashboard see published metadata.
- LiteLLM itself can use the model-cost map for token checks and output adjustment,
  not only display. For example, `get_modified_max_tokens()` uses catalogue-derived
  maxima, and I/O token-rate checks can fall back from `max_output_tokens` to
  `max_tokens`. Whether a specific check runs depends on configuration/call path;
  an internal reader does not prove it is enabled on our active path.
- OpenClaw's audited discovery reads `/v1/models` or `/models`, not `/model/info`.
  Its configured budgets come from our OpenClaw projection.
- Our Agentplane launch adapters do not obtain their context overrides from
  `/model/info`. They read runner-owned configuration.
- Claude and Codex have their own model recognition/catalogues. Removing a LiteLLM
  metadata key does not remove a harness's built-in assumption.

Therefore response filtering and changing LiteLLM's internal catalogue are different
changes. Do not solve one by silently changing pricing or request behavior in the other.

## Appendix C. Client-specific budgets and investigations

Numbers below are preserved configuration choices at draft #9034 head `26862c4395`,
not newly validated provider limits and not a claim that the draft is deployed.

The draft retains a provider limit pair only for the direct Google Gemini models:
1048576 input / 65536 output, based on provider documentation. Historical GPT-5.6
subscription probing accepted 370629 input tokens and rejected 372194; it did not
establish an output ceiling or a combined capacity. The 372000 client budget is
not that complete contract. Astra's 872000 came from Codex client metadata; Sol/Luna
inherited earlier budgets rather than independent probes. Antigravity's upstream
`maxTokens` semantics were ambiguous. Those are reasons to leave provider facts
unset, not reasons to quietly fall back to raw-API catalogue facts.

### Claude Code wrappers

`model_catalog/nix.py` → `claude-wrappers.json` →
`nix/home/claude_code/gateway.nix` maps:

- `maxContextTokens` → `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
- `maxOutputTokens` → `CLAUDE_CODE_MAX_OUTPUT_TOKENS`.

| Wrapper                                           | Context setting | Output setting |
| ------------------------------------------------- | --------------: | -------------: |
| `codex-claude` (Astra primary / Luna small model) |          872000 |         128000 |
| `gemini-claude`                                   |         1048576 |          65536 |
| `antigravity-claude`                              |         1048576 |          65536 |
| `litellm-claude`                                  |         omitted |        omitted |
| `tana-claude`                                     |         omitted |        omitted |

In the inspected Nix Claude Code **2.1.283**, the context override is an assumed
pre-reserve window for custom models; recognized model metadata can take precedence.
The normal observed compaction calculation subtracts an output reserve capped at
20000, then 13000 headroom. Other overrides and execution modes can alter this.
It is neither a plain max-input ceiling nor a universal backend context capacity.

`litellm-claude` also uses a `[1m]` name suffix as a **Claude client convention**.
The suffix is stripped before sending the model name upstream. Switching models
through gateway discovery can lose that convention. Do not create duplicate served
routes merely to encode a client's suffix convention.

Claude Code Web and direct local use must not inherit these gateway overrides just
because they use the same model family. Also, the Agentplane harness-test archive
is **2.1.252**, not 2.1.283: recheck the actual launched package before relying on
version-specific formulas.

### OpenClaw

Audited version: **2026.9.5**, source revision
`ec9c1a13db8938e5a3eaa51fca2e981cde2395a9`.
`contextWindow` is the pre-reserve client context budget; runtime `contextTokens`
can narrow it. Compaction uses `contextWindow - reserveTokens`, with additional
history/pending-input accounting. The core default reserve is 16384; agent policy
raises the floor to 20000, subject to a 25% window cap. It does **not** generally
reserve the full `model.maxTokens`. `maxTokens` is separate output/request metadata.

The public-coder projection currently preserves:

| Selection                                  | `contextWindow` | `maxTokens` |
| ------------------------------------------ | --------------: | ----------: |
| GPT-6 Astra                                |          872000 |      128000 |
| GPT-6 Sol / Luna                           |          372000 |      128000 |
| Direct Google Gemini routes                |         1048576 |       65536 |
| Antigravity Claude Opus / Sonnet           |          200000 |       64000 |
| Antigravity Flash group                    |         1048576 |       65536 |
| Antigravity Pro / Pro Low / Flash Lite 3.1 |         1048576 |       65535 |
| Antigravity GPT-OSS 120B Medium            |          114000 |       32768 |

The 65535/65536 difference is preserved history, not an established distinction in
provider capacity. The 114000 value is an OpenClaw budget, **not** both a provider
input ceiling and a combined context window. Public Coder is now paused (§5); do not validate its entire matrix as a prerequisite
for fixing the active Codex path.

### Codex and Agentplane

Agentplane receives a route-ID-to-budget map through runner configuration
(`model_context_windows`, including the environment and guest-config inputs).
The current cluster override selection is only Qwen IQ4_XS 128K/256K, both wires:
131072 and 262144 respectively. The active GPT subscription routes do **not** receive
872000 or 372000 from this map merely because a wrapper or OpenClaw uses those values.

- Claude adapter sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when an override exists.
- Codex adapter passes `model_context_window` in its startup configuration. Our
  launch helper leaves auto-compaction at Codex's derived default; its comment
  records 90%. Codex also has its own effective-window and metadata rules: do not
  treat that comment as an end-to-end validated threshold for every current binary.
- Changing a model to one with a different configured window, including an absent
  versus present override, requires a new thread. The runner rejects that live switch.
- Harness telemetry is another output: Codex reports
  `thread/tokenUsage/updated.tokenUsage.modelContextWindow`; Claude result frames
  report `modelUsage[*].contextWindow`. Acceptance probes record these as harness
  observations, not backend capacity measurements.

### Active Codex path audit, 2026-10-05

This is a deployment observation, not a new neutral-roster contract. Read-only checks
at approximately 05:34–05:40 UTC examined this audit's **running Agentplane session**,
its native state/usage, deployed LiteLLM configuration, and ready proxy Pods. This
proves one active session, not every agent's version or model selection.

```text
Agentplane runner → Codex app-server 0.157.0
  → central egress → Agentplane LLM ingress /v1/responses
  → shared LiteLLM → CLIProxyAPI /v1/responses → Codex subscription backend
```

The ingress authenticates/replaces credentials and streams the request body unchanged;
it does not currently translate route names or inject context budgets. This pass-through
is an observation, not a required architecture: the operator explicitly permits model
translation at this boundary when useful for native harness behavior. Neither it nor
the runner reads LiteLLM `/model/info` to configure Codex.

| Layer                     | Observed configuration / behavior                                                                                                   | What that means                                                                               |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Native process and thread | Codex **0.157.0**, provider `agentplane`, model `chatgpt/oai-responses/gpt-6-astra`                                                 | A real launched binary/thread, not the native-test archive                                    |
| Launch configuration      | Responses wire to `http://agentplane-llm-ingress.agentplane-staging.svc.cluster.local:8080/v1`; no context/auto-compaction override | GPT budgets are not wired from the roster to this client                                      |
| Recognition               | Native log explicitly warns that the full Astra route uses fallback metadata                                                        | The hidden bare `gpt-6-astra` alias exists in LiteLLM but this session does **not** select it |
| Native telemetry          | Latest sampled `token_count.info.model_context_window`: **258400**; normal tool turns work                                          | Resolved client accounting, not a measured backend limit                                      |
| Deployed proxy config     | Astra: **872000 input / 128000 output**; Sol/Luna: **372000 / 128000**                                                              | Current explicit metadata overrides, with the mixed provenance described above                |
| Live Luna `/model/info`   | Both wires: **372000 / 128000**; legacy `max_tokens`: Responses **128000**, Messages **null**; custom `context_window` absent       | Removing our legacy/custom assignments did not eliminate adapter-dependent fallback           |

Live metadata was read with the substituted **cheap-experiments** key; its view includes
Luna, not Astra/Sol. The latter numbers above are from the deployed ConfigMap, not an
assertion about their `/model/info` responses. Ready LiteLLM Pods ran
`tana-litellm-proxy:devel-20261005030920-bde5d31` (that build pins **1.100.1**).
The deployment does not set `LITELLM_LOCAL_MODEL_COST_MAP`, so its package pin still
does not pin the downloaded catalogue. The ready CLIProxyAPI Pod ran
`cli-proxy-api:devel-20260924021629-ce93c9e`, whose source pin is `7fac6b15bcfe`.

#### What Codex 0.157.0 does with these values

The [matching source](https://github.com/openai/codex/tree/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs)
corroborates the live fallback warning and telemetry:

- `models-manager/src/manager.rs` strips **one** namespace segment for recognition;
  `chatgpt/oai-responses/gpt-6-astra` has two and does not match the bundled Astra entry.
- `model-provider/src/models_endpoint.rs` does not enable API-key catalogue discovery
  for this custom provider without `model_catalog_url`. We do not configure one.
  LiteLLM's metadata fields therefore cannot repair this recognition failure.
- `models-manager/src/model_info.rs` gives fallback models context and override maximum
  **272000**, with **95%** usable: **258400**. `model_context_window` is clamped to the
  recognized entry's maximum; setting 872000 on the unrecognized route would not work.
- `protocol/src/openai_models.rs` derives the default automatic-compaction threshold
  from **90% of the resolved window**: **244800** here, not 90% of 258400. This is a
  source-derived default, not a newly measured compaction boundary or an output reserve
  of 128000. The usable-window headroom covers prompts, tools, and output together.
- Bundled Astra, Sol and Luna entries all default to **272000**, with override maximum
  **872000**. Merely switching to a recognized alias would **not** raise the default
  window. The maximum is a client override bound, not a subscription capacity fact.
- Recognition also changes prompt/reasoning defaults, freeform `apply_patch`,
  `tool_mode=code_mode_only`, and `use_responses_lite`. The tool mode has precedence over
  feature flags when code mode is available. Do not treat an alias as a context-only fix.
- **Version correction to the older audit:** 0.157.0 gates WebSockets on the provider's
  `supports_websockets` (default false), which our provider does not enable. Its JSON
  catalogue still contains `prefer_websockets`, but `ModelInfo` no longer consumes that
  field. Recognition alone does not enable WebSockets in this version.

See `core/src/tools/mod.rs`, `core/src/client.rs`, and
`model-provider-info/src/lib.rs` for those non-context effects. The older
[harness audit](debug/harness_model_metadata.md) remains historical evidence,
not a substitute for inspecting the launched version.

#### Output limits are not enforced by these metadata fields

The current native `ResponsesApiRequest` (`codex-api/src/common.rs`) has no
`max_output_tokens` member. Our runner does not copy the roster's 128000 output value
into requests. Shell-tool `max_output_tokens` controls tool-output truncation, not
model generation length.

More importantly, the deployed gateway's pinned
[Responses translator](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe/internal/translator/codex/openai/responses/codex_openai-responses_request.go)
**deletes `max_output_tokens` and `max_completion_tokens`**, documenting that the
subscription backend rejects those request fields. Its executor defaults to
`https://chatgpt.com/backend-api/codex/responses`. This is source evidence for the
pinned gateway, not a capture of upstream request bodies or private configuration.

One tiny live Luna Responses request asking for `OK` succeeded: **307 input / 5 output
tokens**, status `completed`, response `max_output_tokens: null`. The request supplied
256, but this path must **not** be treated as enforcing that cap. The probe establishes
connectivity only, not input/output ceilings, long-context quality, or arbitrary-client
compatibility. No capacity stress test, alias launch, or live setting change was made.

#### Consequence for the next change

Keep the working Codex launch behavior unchanged while fixing publication. The active
client's 258400 telemetry and LiteLLM's 872000/372000 metadata are separate outputs;
changing one does not update the other. Do not promote either, or the historical
128000 output value, into a justified subscription input/output pair.

Next, run the narrow pinned-LiteLLM unknown/known-pair publication experiment in §4,
including internal request-side consumers. If changing Codex recognition is later
necessary, test the exact ingress-translation, alias or catalogue strategy with native tools, reasoning,
Responses-lite and compaction before enabling it. Do not build a general harness
metadata service to preserve paused consumers. Separate Claude/Codex configuration
vocabularies only when both actually need supported overrides.

### Subscription-path public evidence, 2026-10-05

**A Responses-shaped endpoint does not establish the public API's capacity contract.**
Public research supports that distinction but does not establish our account's backend
ceilings. Sources below are ranked by what they actually establish, not combined into
one supposedly authoritative number.

#### Official API specifications and first-party Codex guidance

The public API model pages for
[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra.md),
[Sol](https://developers.openai.com/api/docs/models/gpt-6-sol.md), and
[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna.md) all list **1050000
combined context, 922000 maximum input, and 128000 maximum output**. Those are API
specifications, not an explicit entitlement for the ChatGPT subscription backend.
The API's 272K long-context pricing threshold is not its capacity ceiling either.

There is also contrary evidence to a blanket claim that subscription Codex cannot use
1M: [Tibo (Codex & ChatGPT at OpenAI), 2026-08-16](https://x.com/thsottiaux/status/2089082893804896524)
explicitly recommends `model_context_window=1000000` and
`model_auto_compact_token_limit=900000` for **GPT-5.6 Sol**, describing the smaller
default as tuned for performance/cost. This establishes advertised intent for that
model, not verified acceptance on our GPT-6 routes. Codex's
[override-bound change](https://github.com/openai/codex/pull/39102) and the inspected
0.157.0 source instead use **872000** as the relevant maximum client override.
Do not silently resolve this discrepancy in favor of either advertised number.

#### Gateway-maintainer account of the subscription backend

[CLIProxyAPI collaborator luispater, 2026-09-15](https://github.com/router-for-me/CLIProxyAPI/issues/5835#issuecomment-5672721746)
says Astra's actual limit is **1M total: 872000 context + 128000 output**, and calls
the catalogue's `context_length: 272000` OpenAI's recommended display value. This is
a useful subscription-specific claim, but **not an OpenAI serving contract or our own
boundary measurement**. Preserve the source's word “context”: do not quietly recast
it as a proven independently attainable maximum input. “Display only” also cannot
be generalized to our client: Codex demonstrably uses metadata to constrain budgets.

The current [CLIProxy public registry](https://github.com/router-for-me/models/blob/main/models.json)
lists GPT-6 Astra/Sol/Luna in applicable subscription groups with `context_length:
272000` and `max_completion_tokens: 128000`. That mutable snapshot is another metadata
source, not a capacity measurement or proof of which snapshot our deployed gateway uses.

#### Reproductions and historical reports, not provider guarantees

- **Client clamping:** an [Astra ChatGPT-sign-in reproduction](https://github.com/openai/codex/issues/41325#issuecomment-5746583842)
  and a [Sol report](https://github.com/openai/codex/issues/47805) show default 272000,
  override maximum 872000, and 95% usable. Requesting 1000000/1050000 yields **828400**
  reported usable tokens; requesting 800000 yields 760000 in the Sol report. These
  corroborate client behavior, **not backend rejection above 872000**.
- **Catalogue variability:** [#40258](https://github.com/openai/codex/issues/40258)
  reports different Sol caps for the same account depending on client-identification
  headers; the reporter says that incident resolved on 2026-09-02.
  [#39144](https://github.com/openai/codex/issues/39144) reports account differences.
  Neither proves a current incident, and header spoofing is not our proposed fix.
- **Historical 372000:** gateway-maintainer explanations in
  [#4195](https://github.com/router-for-me/CLIProxyAPI/issues/4195#issuecomment-4937617812)
  and [#4476](https://github.com/router-for-me/CLIProxyAPI/issues/4476#issuecomment-5034688225)
  concern GPT-5.6 catalogue values and acceptance beyond its smaller advertised
  budget. They do not establish GPT-6 Sol/Luna input ceilings.
- **Inference lower bound:** [#43685](https://github.com/openai/codex/issues/43685)
  reports successful Astra subscription-backed requests with **260307 and 300307
  prompt tokens**. This is third-party evidence of acceptance, not a measured maximum
  or proof of long-context attention quality. Its reported short output cap does not
  establish cap enforcement through our pinned translator.

#### Decision and remaining evidence

There is no justified universal subscription input/output pair to install from these
sources. In particular, do not copy the API's 922000/128000, promote our historical
372000/128000, or subtract 128000 from a native client budget to invent a ceiling.
The Astra maintainer claim is a candidate contract requiring corroboration; it is not
permission to extend it to Sol/Luna. Keep runtime values unchanged in this research
update, and treat unjustified provider limits as unknown in the proposed pair-or-none
publication policy.

A next non-inference check would be a sanitized catalogue for the actual upstream
account and client path, through an explicitly authorized credential-substitution
route; that still establishes only advertised metadata. The public-internet grant
does not grant upstream account credentials. Any backend boundary probes require a
separate quota/cost budget and authorization: the deployed gateway strips output caps,
so requesting a tiny output is not a reliable cost bound. No near-limit inference,
credential-file inspection, or header-spoofing experiment was performed for this research.

### Opt-in long-context experiment and costs

The operator prefers a **separate experimental Agentplane model option**, then a real
working session allowed to accumulate context, over a synthetic near-limit probe suite.
Keep the ordinary option/default intact. This is a client launch preset for the same
backend model, not a new provider model or evidence for different neutral-roster limits.
Reuse the existing offering/configuration machinery where possible; do not build another
registry or add a LiteLLM route unless the actual selection/recognition wiring requires it.
This section records the plan, not an enabled option.

**Model identity need not pass through unchanged.** Agentplane's selected offering,
the harness-facing native model ID, and the LiteLLM routing ID serve different purposes.
The operator explicitly permits changing ingress translation rather than contorting
LiteLLM aliases or requiring native clients to recognize our routing namespace. One
candidate, subject to a native-client smoke test, is:

```text
Agentplane option: Astra / Astra — long context (experimental)
  → Codex model: gpt-6-astra, with the option's client budget
  → Agentplane ingress translates the model to: chatgpt/oai-responses/gpt-6-astra
  → existing LiteLLM route and subscription backend
```

Both options can share that native identity and backend route; only their launch
budget differs. Keep this mapping with the existing Agentplane offering/launch
configuration, not in a new registry or a provider-capacity declaration. Route selection
must remain authorized and unambiguous if multiple offerings use the same native slug;
a caller-supplied native name must not bypass the selected route's access controls.
Check response model identifiers, model switching, resume and telemetry as well as
request rewriting. Native recognition still has the prompt/tool side effects noted
above; translation is not automatically a context-only change. The same boundary is
available for future Claude support, without implementing that paused path now.

- Give the option an honest label such as “Astra — long context (experimental)”, not
  “1M guaranteed”. Start a fresh native thread; do not mutate an existing thread's budget.
- Resolve model recognition explicitly, including the tool/prompt behavior described
  above. Merely requesting 1000000 on our current unrecognized route stays clamped.
  The inspected recognized-model maximum is **872000 configured / 828400 usable**;
  Tibo's 1000000 recommendation concerns a different model/version context and does not
  override that clamp. Recheck the exact launch version before choosing the preset.
- Record the effective budget and compaction threshold at startup. With the inspected
  default 90% policy, an 872000 window starts automatic compaction at **784800**.
  Normal use may therefore encounter compaction before a backend limit; that is useful
  operational evidence, but not a maximum-capacity test. Raising/disabling compaction
  would be a separate deliberate experiment, not an incidental preset change.
- During ordinary work, record input/cached-input/output and reasoning usage when
  available, latency, quota before/after, compaction events, and errors. Distinguish a
  native-client clamp, context rejection, quota exhaustion, proxy timeout, and degraded
  recall. Stop on trouble rather than repeatedly retrying a large failing request.

#### How expensive is a long-context probe?

There are **three different ledgers**: upstream included subscription allowance or
purchased credits, hypothetical direct-API spend, and LiteLLM's local spend accounting.
None is automatically a conversion formula for the others. These examples use the
public rates retrieved **2026-10-05**, Standard speed, text-only, and one inference
request. A user-visible agent turn can contain many requests; retry and compaction
requests also contribute usage. Increasing the configured window alone sends no tokens.

**On our subscription path**, included usage is not billed as an equivalent raw-API
request. The [Codex pricing page](https://learn.chatgpt.com/docs/pricing#token-rates)
explicitly says neither API prices nor credit prices determine included allowance
consumption. It depends on context, reasoning, tools, caching and other factors. We
cannot honestly predict “this probe consumes X% of your plan” from prompt size alone.
Purchased-credit rates do allow a conditional estimate:

| Model       | Credits / 1M uncached input | Credits / 1M cached input | Credits / 1M output | 800K uncached input + 1K output |
| ----------- | --------------------------: | ------------------------: | ------------------: | ------------------------------: |
| GPT-6 Astra |                         250 |                        25 |                1250 |                  201.25 credits |
| GPT-6 Sol   |                          50 |                         5 |                 250 |                   40.25 credits |
| GPT-6 Luna  |                         2.5 |                      0.25 |                12.5 |                  2.0125 credits |

These are the published Standard credit rates, not a claim that our account is using
purchased credits. The page has no separate cache-write charge for Codex credits and
warns about legacy Enterprise rate cards. Do not import the raw API's long-context
multiplier into this credit calculation. Credit purchase prices depend on the plan or
agreement; actual included-quota impact needs before/after observations on the account
that served the request, accounting for concurrent activity.

**As a paid-API comparison only**, the model pages linked above apply 2× input/cache
rates and 1.5× output rates to the **whole request** above 272K input tokens:

| Model       | 300K cold input | 800K cold input | 872K cold input | 800K fully cache-hit input | Additional 1K output |
| ----------- | --------------: | --------------: | --------------: | -------------------------: | -------------------: |
| GPT-6 Astra |           $6.00 |          $16.00 |          $17.44 |                      $1.60 |               $0.075 |
| GPT-6 Sol   |           $1.20 |           $3.20 |          $3.488 |                      $0.32 |               $0.015 |
| GPT-6 Luna  |           $0.06 |           $0.16 |         $0.1744 |                     $0.016 |             $0.00075 |

Cold-input examples use ordinary uncached-input prices, excluding any separately
charged cache writes, tools, images, regional/speed premiums or tax. Fully cache-hit
figures are an idealized comparison, not a promise of cache eligibility or retention.
Billable output includes applicable reasoning tokens, not just visible response text.
For example, 800K cold input plus 1K output is **$16.075 Astra / $3.215 Sol /
$0.16075 Luna** on that API rate card. A successful Luna test would not prove Astra's
capacity. Ten independent cold 800K Astra requests cost **$160 input alone**; one cold
plus nine fully cache-hit requests would instead cost **$30.40 input**, before any
other charges. One probe and a multi-step session are very different budgets.

Our gateway strips request output caps (Appendix C), so “reply OK” is an instruction, not a
hard cost bound. At the API comparison rates, 128K output would add $9.60/$1.92/$0.096
respectively; this arithmetic does not verify a subscription output ceiling. The
practical concern is shared subscription quota and repeated long turns, not that every
single probe must be prohibitively expensive. No long-context request was sent for
these estimates, and no experimental option has yet been enabled.

## Appendix D. Ollama serving configuration

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
