# Model-roster migration inventory

Companion to the [design](design.md). This records files and consumers requiring
review, not a commitment to change every file. **Only explicitly approved dispositions
are decisions**; all other keep/move/merge/delete options remain open.
The [tracking issue](https://github.com/agentydragon/ducktape/issues/9574) owns PR/rollout
status and links to the historical parked-integration inventory in
[#9121](https://github.com/agentydragon/ducktape/issues/9121).

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

**Source snapshot, reconciled 2026-10-10:** 64 of 80 public entries publish explicit
metadata: 60 generative pairs and four embedding input-only declarations. This is
publication coverage, not a percentage of programme completion or validated capacity.
The 12 ChatGPT entries below count as published despite their provisional provenance.

Direct-provider chat, Antigravity text, the refreshed Claude subscription roster and
embedding declarations have landed. The three larger GPT-OSS OpenAI exposures and
three Tana routes are parked; their restoration constraints below remain in force.
The tracker retains the PR ledger and dated rollout evidence rather than repeating
successive implementation/CI states here.

| Family                                                           | Remaining work                                                                                                                                                                                                                                                                                                       |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ChatGPT GPT-6 and GPT-5.6, both wires (12 published entries)     | Audit the mixed historical/client-derived declarations. Current/legacy aliases are already coherent; they do not validate the numbers. Preserve the active subscription path and consumer budgets.                                                                                                                   |
| Anthropic subscription                                           | Opus/Sonnet/Haiku 5.5 and Fable 5.1 have sourced gateway declarations. Registration/configuration/pricing presence was verified on 2026-10-09 ([record](litellm_metadata.md#claude-subscription-refresh-2026-10-09)); account request success and beta-dependent/full-output/joint capacity remain untested.         |
| Antigravity                                                      | Text publication is complete from the recorded response and gateway convention; Claude/GPT-OSS boundaries and joint capacity remain untested. The one image entry still needs applicable metadata or an explicit disposition.                                                                                        |
| Ollama chat (13 unpublished entries)                             | Installed GGUF context facts are recorded ([audit](litellm_metadata.md#ollama-gguf-context-audit-2026-10-09)). Choose defensible publication semantics: GGUF context and requested `num_ctx` are not an input/output pair. Larger native GPT-OSS variants remain; the audit does not prove their labeled capacities. |
| Groq transcription (two unpublished entries)                     | Research applicable audio metadata and projection behavior without inventing a generative output ceiling.                                                                                                                                                                                                            |
| Embeddings (four published entries, including the durable alias) | Declarations are complete. Ollama full-input/boundary verification with truncation disabled remains explicitly deferred; batch/special-token limits may be lower ([record](litellm_metadata.md#ollama-embedding-input-metadata-2026-10-09)). Preserve dimensions and stored indexes.                                 |

These are **remaining data/disposition decisions**, not a second runtime registry.
The `publish_limits` flag remains transitional until retained declarations and
mode-appropriate publication rules are complete. Metadata research does not block
behavior-preserving consumer simplification. The tracker owns lane ordering and
current PR/rollout status; this inventory owns unresolved file/data decisions.

### Parked GPT-OSS 20B OpenAI exposures

Approved 2026-10-09: remove only `ollama/oai-chat/gpt-oss-20b-{256k,512k,1m}`
from serving, derived key allowlists, and Agentplane staging/testing offerings.
Those exposures used the base `gpt-oss:20b` tag without baked-context aliases;
Ollama's OpenAI-compatible endpoint ignores native `options.num_ctx`.

Retain all three `ollama/olm-chat/` counterparts with their explicit request options,
both 128K wires, other models, existing fallback/default choices, and the unchanged
Ollama provisioning code, models and storage. This removes exposures, not stored
state or the ability to make native requests. Existing sessions configured to use a
parked route will need a retained compatible route after rollout; there is no silent
rerouting to the base model or to the native wire.

To restore: deliberately provision/select appropriate baked tags (or another proven
OpenAI-compatible context-selection mechanism), verify the effective serving path,
then restore the selected exposures in `catalog.py` and regenerate consumers. A tag
name or successful oversized request is not capacity proof. No alias provisioning,
model pruning, client-budget increase or automatic unpause is approved by this change.
[#9574](https://github.com/agentydragon/ducktape/issues/9574) tracks source/rollout status.

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
| Tracking issue #9574                              | Current work/PR status; links to #9121's historical parked-state inventory and restoration instructions                                                                                         |

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

### Tana exposure parked (2026-10-09)

Tana is a reverse-engineered gateway with unknown limits; vendor model names are
not evidence of equivalent capacity. Temporarily omit its three routes from the
served roster rather than invent metadata. The Tana key lane/fallback and generated
Claude wrapper selection are removed together. The dedicated client key and team
are removed from Terraform; the client key revocation and successful reconciliation
were verified, but this is not a claim that every historical same-alias team was deleted.
Key/team identity and accounting continuity are not preserved. Their encrypted
credential files, backend credentials, gateway implementation/registration,
wrapper renderer and stored application data remain; no deployment is paused or deleted.

The initial blocked-key approach in #9587 applied the block and cleared the team
fallback, but Terraform failed to converge: the provider rewrites `models = []`
for a team-associated key to `["all-team-models"]`, contradicting the planned empty
list. Removing the unused key/team avoids that provider mismatch without unblocking
or manually editing Terraform state.

[Operator verification after #9592](https://github.com/agentydragon/ducktape/issues/9574#issuecomment-6082223649)
confirmed successful Terraform apply at `6599c2eb1ecfc1477dea10dc271ac36981dbdaa4`
and zero `tana-clients` keys. The subsequent operator-provided query showed four
older same-alias teams with no attached keys or projects, still carrying old
fallbacks. They remain a separate cleanup decision: these counts do not establish
all dependencies or authorize deletion by alias. Do not keep the completed key
revocation open waiting for unrelated historical-row cleanup.

TODO(#9574): before re-enabling, review whether the gateway can support defensible
metadata (or explicitly accept unknown limits), restore the served routes and
consumer selections, and recreate the client key/team with a reviewed allowlist
and fallback. Retained encrypted key material alone does not authorize access.
No inference or capacity testing was performed as part of parking.
