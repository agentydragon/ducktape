# Model-roster migration inventory

Companion to the [design](design.md). This records files and consumers requiring
review, not a commitment to change every file. **Only explicitly approved dispositions
are decisions**; all other keep/move/merge/delete options remain open.
[Historical issue #9574](https://github.com/agentydragon/ducktape/issues/9574) records
dated PR and rollout evidence and links to the earlier parked-integration inventory in
[#9121](https://github.com/agentydragon/ducktape/issues/9121).

## Current sequencing checkpoint

See the [routing review checkpoint](routing_review.md#work-ordering-routing-decision-before-further-roster-implementation)
for the sequencing decision and the
[gateway replacement requirements](../cluster/docs/plans/x/llm_gateway_replacement.md)
for the later consumer inventory and evaluation constraints. Crossplane owns the
existing keys and teams separately from any gateway replacement. See the
[Crossplane recovery record](../cluster/debug/2026_10_22_litellm_crossplane.md) for
rotation and handoff evidence.

## Consumer inventory and side effects

Before deletion or renaming, check these as well as the obvious clients:

- Crossplane virtual-key allowlists, team fallback order, and experiment keys.
- Agentplane picker/default selections, LLM ingress metadata/auth routing, and
  runner environment/guest configuration. These are not interchangeable registries.
- Hidden Codex recognition aliases and the public embedding compatibility alias.
- OpenClaw durable memory: stored embedding identities can outlive a running agent.
  Pausing the app is not permission to delete/rebuild its index or change its model.
- Gatus's selected Ollama probe, inference/probe scripts, and live harness acceptance
  cases.
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
| `cluster/cdk8s/litellm/crossplane_keys.py`                                                                                 | Crossplane reads `KEY_MODEL_LANES` and owns keys and teams.                                                                                                                                           |
| `tana/litellm_proxy/` (including `custom_handler.py`, `provider.py`, `BUILD.bazel`, `requirements.in`, `requirements.txt`) | Does the chosen publication/adapter solution require changes here at all? Avoid an unneeded proxy framework or incidental dependency upgrade.                                                         |
| `tana/litellm_proxy/model_registry.py`                                                                                     | Which entries are necessary Tana protocol/discovery mappings versus duplicate definitions of our selected models/routes? Do not assume that another “registry” is redundant without tracing its role. |

#### Agentplane selection, projection and runtime

The [cluster wiring guide](../cluster/docs/model_catalog.md#projections) owns
the current app/ingress projections, including the separate Codex catalog and
native Claude budget lookup. Retain named-route offerings and explicit preset
defaults; environments supply selections to the renderers.

Exposing a different model ID from the upstream request remains a design question
in the [ingress contract](../agentplane/llm_ingress/README.md#per-model-client-configuration).
Coordinate lookup and request/response identity, authorization and accounting.

For changes to this wiring, check new-runner startup, sessions and model switches,
plus existing-runner inference and stream continuity. [Historical issue #9574](https://github.com/agentydragon/ducktape/issues/9574)
records the October acceptance evidence and gaps.

#### Nix wrappers and direct local clients

Source review: **retain the current boundary**, not another wrapper migration.
`model_catalog/nix.py` owns wrapper route selections and client budgets, validates
primary/Haiku membership in the key lane, and emits `claude-wrappers.json`.
`nix/home/claude_code/gateway.nix` renders that data into environment/exec settings;
individual wrappers retain family-specific behavior, not duplicate rosters.
The JSON remains generated, not another authored model registry.

The [Home Manager option](../nix/home/modules/claude-gateway-wrappers.nix) installs
wrappers with their client keys by explicit opt-in and defaults to an empty list.
The Tana renderer still requires restoration of its routes and client key. Keep the
retained renderers, credential declarations and direct clients. [#9112](https://github.com/agentydragon/ducktape/pull/9112)
records the original pause; verify installation and behavior on the workstation separately.

#### Ollama serving variants

| Current file(s)                                                                                                                                            | Question to resolve                                                                                                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/cdk8s/ollama/app.py`                                                                                                                              | Where should server defaults and serving-variant configuration be declared, and what should deployment rendering consume?                                                                                                 |
| `cluster/cdk8s/ollama/setup-gpt-oss-v2.sh.j2`                                                                                                              | Now rendered from Ollama source declarations; shell remains the executor. Further script/interface changes remain open.                                                                                                   |
| Ollama source variants in `model_catalog/ollama.py`; route selection and serialization in `model_catalog/catalog.py` and `cluster/cdk8s/litellm/config.py` | Decide jointly with the two files above: who defines variant identity, upstream tag and requested context, and how each wire actually applies them? Do not infer provider limits or client budgets from serving settings. |

#### Public Coder and smaller consumers

Reviewed 2026-10-10; no structural rewrite justified:

- `cluster/cdk8s/public_coder/config.py` owns the paused OpenClaw client's named
  route selections, labels and explicit context/output budgets. `app.py` serializes
  that config; workload, egress, devbox and backup modules are not model rosters.
  Keep the renderer and embedding route identity. The current default is GPT-6
  Luna, not a capacity measurement; the stale “measured 5.6” comment is corrected.
- `cluster/cdk8s/gatus/config.py` selects the named GPT-OSS 20B 128K route for its
  probe. It does not reconstruct route slugs or maintain a second model catalogue.
- `model_catalog/policies.py` owns key lanes and fallback subsets;
  `cluster/cdk8s/litellm/crossplane_keys.py` serializes them, and Crossplane owns explicit
  key/team/accounting bindings. Combining lanes is authorization policy, not
  duplicate model identity. Keep these boundaries.

Existing roster, Nix, key-policy and OpenClaw tests check identity, authorization,
serialization and native-consumer assumptions. Preserve them; do not add tests that
merely copy the roster constants. This is source-disposition review, **not** live
startup/model-switch acceptance, pause verification, storage cleanup or permission
to restore consumers. Historical issue #9574 records the dated operational evidence and gaps.

#### Generated outputs, tests, build boundaries and documentation

| Current file(s)                                                                                                                                                                               | Question to resolve                                                                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Affected generated cluster manifests and Nix JSON artifacts                                                                                                                                   | Which outputs follow from the eventual source changes? Regenerate them from their owners, never make them another authored source.                                                             |
| `cluster/cdk8s/test_model_rosters.py`; `cluster/cdk8s/litellm/test_config.py`, `test_openclaw_models.py`; `model_catalog/test_nix.py`, `test_policies.py`; affected Agentplane/OpenClaw tests | Which tests prove identity, authorization, fallback, serialization or native-client behavior, and which merely restate fields? Decide retention/consolidation based on that distinction.       |
| Affected `BUILD.bazel` files, including `cluster/cdk8s/BUILD.bazel`, `cluster/cdk8s/litellm/BUILD.bazel`, `cluster/cdk8s/agentplane/BUILD.bazel`                                              | Which dependency/visibility edges should change as ownership is decided? Preserve the cdk8s generation/runtime boundary.                                                                       |
| `model_catalog/design.md`; `model_catalog/README.md`; `cluster/docs/model_catalog.md`                                                                                                         | The neutral design/research split and cluster operations-guide scope are settled. See [documentation consolidation](#documentation-ownership); preserve evidence and restoration instructions. |

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

The [routing review](routing_review.md#why-reopen-the-design) records the
all-routes publication proposal and why it remains under reconsideration.
The October 2026 snapshot below preserves the evidence and decisions still relevant
to evaluating that proposal.

**Source snapshot, reconciled 2026-10-10:** 58 of 74 public entries publish explicit
metadata: 54 generative pairs and four embedding input-only declarations. This is
publication coverage, not a percentage of programme completion or validated capacity.
The six ChatGPT entries below count as published despite their provisional provenance.
GPT-5.6 Sol/Terra/Luna have been removed from both ChatGPT wires and derived key
allowlists; GPT-6 selections and consumer budgets are unchanged.

The snapshot includes direct-provider chat, Antigravity text, the then-current
Claude subscription roster and embedding declarations. The three larger GPT-OSS OpenAI exposures and
three Tana routes are parked; their restoration constraints below remain in force.
See the historical tracker for the dated PR/rollout ledger. For a later subscription
roster refresh and bounded route checks, see [#18716](https://github.com/agentydragon/ducktape/pull/18716).

| Family                                                           | Remaining work                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ChatGPT GPT-6, both wires (six published entries)                | The [2026-10-10 source recheck](client_budgets.md#gateway-maintainer-account-of-the-subscription-backend) found no justified replacement pair. [Two approved Luna probes](client_budgets.md#luna-subscription-path-probes-2026-10-10) establish 900419 accepted input, not an exact maximum; the ~990K failure masked its cause. Publication policy remains open; client budgets stay unchanged. |
| Anthropic subscription                                           | Opus/Sonnet/Haiku 5.5 and Fable 5.1 have sourced gateway declarations. Registration/configuration/pricing presence was verified on 2026-10-09 ([record](litellm_metadata.md#claude-subscription-refresh-2026-10-09)); account request success and beta-dependent/full-output/joint capacity remain untested.                                                                                     |
| Antigravity                                                      | Text publication is complete from the recorded response and gateway convention; Claude/GPT-OSS boundaries and joint capacity remain untested. The one image entry still needs applicable metadata or an explicit disposition.                                                                                                                                                                    |
| Ollama chat (13 unpublished entries)                             | Installed GGUF context facts are recorded ([audit](litellm_metadata.md#ollama-gguf-context-audit-2026-10-09)). Choose defensible publication semantics: GGUF context and requested `num_ctx` are not an input/output pair. Larger native GPT-OSS variants remain; the audit does not prove their labeled capacities.                                                                             |
| Groq transcription (two unpublished entries)                     | Research applicable audio metadata and projection behavior without inventing a generative output ceiling.                                                                                                                                                                                                                                                                                        |
| Embeddings (four published entries, including the durable alias) | Declarations are complete. Ollama full-input/boundary verification with truncation disabled remains explicitly deferred; batch/special-token limits may be lower ([record](litellm_metadata.md#ollama-embedding-input-metadata-2026-10-09)). Preserve dimensions and stored indexes.                                                                                                             |

The table is dated and distinguishes open evidence gaps from configuration. Read the
current declarations for each served route. The
[publication contract](litellm_metadata.md#publication-ownership) describes the
existing `publish_limits` projection and the decisions still needed to change it.

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
[#9574](https://github.com/agentydragon/ducktape/issues/9574) records the original source and rollout evidence.

## Documentation ownership

Retain [`cluster/docs/model_catalog.md`](../cluster/docs/model_catalog.md) as the
cluster wiring and operations guide. It owns deployment bindings, projection and
regeneration entry points, and cluster-specific pause/restoration instructions.
Neutral model semantics stay outside `cluster/`; the guide links to them rather than
maintaining a second specification.

| Document                                        | Responsibility                                                                              |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `model_catalog/design.md`                       | Cross-consumer ownership, constraints and unresolved design choices                         |
| Linked limits/client-budget research            | Dated supporting evidence and semantic investigations, not independent activation decisions |
| `model_catalog/README.md`                       | Neutral-package entry point and generation entry points                                     |
| `cluster/docs/model_catalog.md`                 | Landed cluster wiring and operations; not proof of live rollout                             |
| `model_catalog/debug/harness_model_metadata.md` | Historical version-scoped cross-layer audit, not current deployment policy                  |
| Historical issue #9574                          | Dated PR/rollout evidence and original parked-integration obligations                       |

Update affected wiring descriptions with implementation changes. Remove resolved
alternatives and completed instructions instead of appending migration histories;
preserve useful evidence and restoration requirements. Remaining metadata decisions,
consumer dispositions and runtime acceptance are not closed by documentation cleanup.

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
