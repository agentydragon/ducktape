# Model-roster migration inventory

Companion to the [design](design.md). This records files and consumers requiring
review, not a commitment to change every file. **Only explicitly approved dispositions
are decisions**; all other keep/move/merge/delete options remain open.
The [tracking issue](https://github.com/agentydragon/ducktape/issues/9121) owns PR/rollout
status and the complete parked-integration inventory.

## Consumer inventory and side effects

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
| `cluster/cdk8s/agentplane/app.py`                                                                            | How should runner configuration be emitted without treating model token limits as universal client budgets?                                                                                             |
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
| `cluster/cdk8s/ollama/setup-gpt-oss-v2.sh.j2`                                                           | Now rendered from Ollama source declarations; shell remains the executor. Further script/interface changes remain open.                                                                                                   |
| Ollama definitions in `model_catalog/catalog.py`; their projection in `cluster/cdk8s/litellm/config.py` | Decide jointly with the two files above: who defines variant identity, upstream tag and requested context, and how each wire actually applies them? Do not infer provider limits or client budgets from serving settings. |

#### Public Coder and smaller consumers

| Current file(s)                                                                                                                                                                                                                 | Question to resolve                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/public_coder/app.py`                                                                                                                                                                                             | Where should OpenClaw selections, labels and client budgets live? What retained renderer is useful for revival without supporting every paused combination now?      |
| Public Coder workload/storage configuration, including `cluster/cdk8s/public_coder/devbox.py`, `public_coder/proxy.py`, `public_coder/egress.py`, `public_coder/sshpiper.py`, `public_coder/backup.py` and associated manifests | Which files actually depend on roster decisions? No change may incidentally undo the pause, delete retained storage/backups, or change a durable embedding identity. |
| `cluster/cdk8s/parked/haku_openclaw_spike_config.py`                                                                                                                                                                            | What dependencies and revival information need to remain documented, and is any source change needed while parked?                                                   |
| `cluster/cdk8s/gatus/config.py`                                                                                                                                                                                                 | Is probe selection already a sufficient projection of canonical routes, or does it duplicate naming/selection logic?                                                 |

#### Generated outputs, tests, build boundaries and documentation

| Current file(s)                                                                                                                                                                               | Question to resolve                                                                                                                                                                                    |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Affected `cluster/k8s/…` manifests and Nix JSON artifacts                                                                                                                                     | Which outputs follow from the eventual source changes? Regenerate them from their owners, never make them another authored source.                                                                     |
| `cluster/cdk8s/test_model_rosters.py`; `cluster/cdk8s/litellm/test_config.py`, `test_openclaw_models.py`; `model_catalog/test_nix.py`, `test_policies.py`; affected Agentplane/OpenClaw tests | Which tests prove identity, authorization, fallback, serialization or native-client behavior, and which merely restate fields? Decide retention/consolidation based on that distinction.               |
| Affected `BUILD.bazel` files, including `cluster/cdk8s/BUILD.bazel`, `cluster/cdk8s/litellm/BUILD.bazel`, `cluster/cdk8s/agentplane/BUILD.bazel`                                              | Which dependency/visibility edges should change as ownership is decided? Preserve the cdk8s generation/runtime boundary.                                                                               |
| `model_catalog/design.md`; `model_catalog/README.md`; `cluster/docs/model_catalog.md`                                                                                                         | The neutral design/research split is approved; the cluster guide's fate remains open. See [documentation consolidation](#documentation-consolidation); preserve evidence and restoration instructions. |

### Approved file disposition: cross-layer harness audit

**Previously approved and implemented:** moved
`agentplane/docs/model_metadata.md` to
[`model_catalog/debug/harness_model_metadata.md`](debug/harness_model_metadata.md).
Its LiteLLM naming/authorization, Nix-wrapper and native-client findings span consumers;
Agentplane supplied the capture machinery but does not own all those findings. Preserve
the historical versions and evidence, update relative links, and link from Agentplane's
capture documentation rather than keeping a duplicate or redirect stub. Capture tools
and runtime code stay in Agentplane. This decision does not settle any other file's fate.

## Token metadata ownership migration

**Agreed target:** every retained served route gets its applicable token metadata from
Ducktape declarations. LiteLLM catalogue values may be copied with entry/revision
provenance; live fallback is not the long-term authority. Legacy schema aliases are
projected from the same declaration, not independent facts. Fresh probing of every model
is not required. This does not reopen consumer-budget ownership or authorize route removal.

Source inventory at #9273's base (`3822ce017a`): **90 public entries, 12 with explicit
input/output overrides**. The first no-patch slice completes those 12 overrides with the
legacy output alias. GPT-5.4/5.5 on both wires are now retired by operator request,
leaving 86 public entries. The 33 direct Anthropic/Gemini/Mistral/Groq chat entries
now also publish sourced pairs; see the [source ledger](litellm_metadata.md#direct-provider-sources-2026-10-05).
Thus 45 entries have explicit overrides; the remaining migration is not complete:

| Routes                                             | Source / next decision                                                                                                                                                                                                                                |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ChatGPT GPT-6 and GPT-5.6, both wires (12 entries) | Existing configured numbers remain; complete schema aliases now. Their mixed historical/client provenance still needs the shared-limit semantic cleanup.                                                                                              |
| Anthropic subscription; Tana Claude                | Account/gateway paths need an explicit source choice. Same vendor slug alone does not establish equivalence to direct-API metadata.                                                                                                                   |
| Antigravity                                        | Adopt the already-recorded fresh Google response deliberately, retaining family-specific semantics. Flash Lite 3.5 now has a pair; image output has no pair in that response. No extra probes are needed just to rediscover this.                     |
| Ollama chat variants                               | `num_ctx` is allocation, not an input/output pair. GPT-OSS 20B 256K/512K/1M OpenAI-wire routes lack corresponding baked aliases. Choose meaningful served metadata or ask which variants to pause; do not turn route-name sizes into capacity claims. |
| Gemini and Ollama embeddings; Groq transcription   | Declare applicable metadata by mode. Gemini input ceilings already have source comments; embedding dimensions and audio constraints are not generative output-token limits. Preserve the durable embedding alias.                                     |

These are **remaining data/disposition decisions**, not a second runtime registry.
The `publish_limits` flag is transitional and should disappear once retained declarations
are complete. Update this inventory as implementation lands; keep PR status in #9121.

## Documentation consolidation

[`cluster/docs/model_catalog.md`](../cluster/docs/model_catalog.md) is part of the
refactor, not a second specification to leave untouched beside this design. **One
candidate is to retain it as a smaller cluster wiring and operations guide; its exact
fate and proposed operational responsibility remain undecided.** Do not move the neutral
catalogue's ownership back under `cluster/`, or maintain parallel explanations of
model-limit semantics in both places.

| Document                                          | Responsibility / decision status                                                                                                                                                                |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model_catalog/design.md` and its linked research | **Topic split approved:** design owns cross-layer constraints and unresolved choices; linked research owns dated supporting evidence                                                            |
| `model_catalog/README.md`                         | Short neutral-package entry point: module responsibilities, generation entry points, links to the design and deployment guide                                                                   |
| `cluster/docs/model_catalog.md`                   | **Candidate, not approved:** current cluster bindings and projections, where to change deployment selections, regeneration/check commands, and cluster-specific pause/restoration procedures    |
| `model_catalog/debug/harness_model_metadata.md`   | **[Location approved](#approved-file-disposition-cross-layer-harness-audit):** historical version-scoped cross-layer audit, linked as evidence rather than treated as current deployment policy |
| Tracking issue #9121                              | Work/PR status and complete parked-integration inventory, linking to the relevant restoration instructions                                                                                      |

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

As decisions land, record the approved dispositions here and remove resolved alternatives and
completed rollout checklists; retain only useful dated evidence/rationale. Do not copy
this entire design into the cluster guide, create a third overview, or keep two current
specifications. Completion means the documentation
is accurate and non-duplicated, neutral semantics have one home, and paused integrations
remain recoverable.
