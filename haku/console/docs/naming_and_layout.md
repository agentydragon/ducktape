# Naming and layout

The blessed target for the rename/reorganization batch that the #4772 vocabulary-collapse
umbrella governs (#4772 collapse, #4924 domain packaging, #4836 identity roles, #4838 grant
vocabulary, #4865 de-Haku, #4887 indexer exit, #4918 tool naming, aligned to the #4667
neutral-operation protocol). It is the reference every burn-down PR cites: the target package
layout (§2), the canonical terminology (§3), the reviewer checklist (§4), the binary-to-package
dependency contract (§5), and the burn-down sequencing (§6).

A plan cannot be cited (<../../../README.md> § plans/); a doc under `docs/` can. This is that
citable home. When a burn-down PR renames or moves something, it names the row here it satisfies;
when a name lands or a chunk completes, this doc is updated to match.

## 1. Status and the two pending name-picks

Two names are **deliberately unresolved** — they are the operator's to pick, and every downstream
PR inherits them. They appear as explicit placeholders **everywhere they occur** in this doc, and
no burn-down PR hardcodes a guessed value for either. When one is blessed, it lands here and in the
PRs that were waiting on it.

- **`<platform>`** — the de-Haku platform name (#4865). The console is a general agent-hosting
  control plane misnamed after the one configured agent called "Haku". `<platform>` is the name the
  tree, the deployments/namespaces, the connector, and the docs re-root onto; "Haku" survives only
  as one configured agent/tenant. Broader than console — recorded once blessed at the platform level
  and referenced here.
- **`<auth-context>`** — the identity **authentication-context** family name (#4836). "Authentication
  context" is the working framing for the composed actor type (`AgentActor` = request principal plus
  the accountability identities authentication established). The final name is pending; the role and
  its shape are settled (§3).

Everything else in this doc is a settled direction. Where a target module, tree, or class does not
exist on `devel` yet, it is the forthcoming destination of an in-flight issue, called out inline.

## 2. Target tree

The end state for the console app is domain packages, not the current ~90-module flat top level plus
a ~30-module `x/` chat subtree. The indexer **worker** exits console entirely to `haku/indexer/`
(#4887); `x/` **dissolves** as its modules graduate into `conversation/` and `session/`
(`channels/` has already graduated), while the harness adapters' native projection heads
**runner-ward** (#4667), leaving only a small
`harnesses/` selection residue console-side (§2). The whole tree later re-roots under
`<platform>/console/` (#4865, the
last chunk).

Representative modules are shown with their current source annotated, not all ~100. The rule that
governs every filename and entity inside these packages — drop the package prefix — is §4.1; this
tree shows the packages, §3 and §4 settle what the files and classes inside them are called.

```text
<platform>/console/                                    # re-rooted from haku/console/ last (#4865)
  app.py  config.py  deps.py  deployment.py  capabilities.py          # app shell (stay top-level)
  database_schema.py  database_migrate.py  migrations/  pydantic_column.py  models.py
                                                        # schema stays central; ORM row classes
                                                        #   carry the …Row suffix at definition (§3)

  identity/            # canonical Operator/Agent authority — see <agent_authority.md>
    operator.py  operator_auth.py  operator_login_flow.py  authentik_operator_token.py
    operator_identity.py  operator_identity_store.py  operator_agents.py
    agent.py  enrollment.py  enrollment_routes.py  naming.py  authorization.py   # from agents/
    agent_bearer_authority.py  credential_binding.py
    fastmcp_adapter.py            # from mcp_auth/
    mcp_agent_auth.py
    authentication_context.py     # the `<auth-context>` composed actor family (was tool_call_actor.py) [#4836 part 1]
    request_principal.py          # the RequestPrincipal atom, split out of grants/principal.py

  grants/              # shared-envelope grant model (#4889, landed)
    principal.py       # GrantPrincipal family + RequestPrincipal + applies_to; RequestPrincipal leaves with C10
    envelope.py        # the shared envelope: GrantStatus + derive_status, the Grant*Error family,
                       #   GrantEnvelope model base, GrantEnvelopeColumns mixin + per-table envelope
                       #   constraints, applicability clause, idempotent grant-set replay, window/batch validators
    provenance.py      # manual-approval source-ToolCall invariant (reads database_schema, so split from envelope)
    kubernetes/        # models, repository, service, routes, authorization, kubectl_passthrough_policy,
                       #   proxy_authorization (file prefixes dropped; entity names wait on §4.1 seam 3)
    http/              # models, repository, service, routes, decide_{config,routes,service}

  mcp/                 # the approval / audit / execution surface
    server.py  approval.py  config.py  catalog_reconciler.py  guidance.py  mount.py  reflection_cache.py
    tool_call_service.py           # the actor-scoped lifecycle boundary
    execution.py                   # McpExecutionCaller/Context (was mcp_execution.py)  [#4836 parts 2-3]
    in_process_servers.py  in_process_server_access.py  operator_oauth.py  export_tool_schemas.py
    tools/                         # in-process tool servers (gmail, google_calendar, kubernetes, hostexec, …)
      recall/                      # the index-read tool server — reader.py + access.py + the tool module (was
                                   #   recall_index_reader/recall_index_access + tools/recall_index.py). Console keeps
                                   #   ONLY the query-time read path (#4887); its SOLE consumer is the haku_index MCP
                                   #   server (nothing reads the index over SPA/HTTP), so it folds in with its peers
                                   #   under tools/, not a standalone package (§4.2). Tool-id renamed under #4918/de-Haku.
    auto_approval/

  oauth/               # provider account-linking + operator OAuth token machinery
    provider_connection.py  provider_connection_registry.py  token_state.py  token_support.py
    connection_result.py  association_maintenance.py  callback_page.py

  notifications/       # Web Push pending-approval domain + the wake wires
    push.py  push_routes.py  console_events.py  connection_metrics.py
    pg_wake.py  session_wakes.py  conversation_wakes.py

  hostexecd/           # console-side registry / machine-API for the hostexecd fleet — NOT node_daemons (§3)
    service.py  models.py

  conversation/        # the durable, provider-neutral record (landed)
    conversation_event.py          # ONE Pydantic vocabulary: row body + wire (#4772 core, landed C5);
                                    #   x/conversation_events.py is the neutral in-memory vocabulary that bridges into it
    reads.py  reader.py  log.py  follow.py  history.py  live_updates.py  runtime.py
    item_reads.py                  # folded item read models ("entry" leaves with C6) — private to the
                                   #   conversation read surface (the store/reader that produce them + the
                                   #   haku_conversations tool that serves them); NOT a generic mcp/ file (§4.2)
    prompt_inbox.py  prompt_origin.py  journal_consumer.py
                                   # the durable prompt inbox + origin vocabulary + the #4667 journal commit

  session/             # one runner incarnation + its wire log (landed)
    store.py  runtime.py
    session_frames.py  status.py   # the wire-log and lifecycle vocabularies (BridgeFrameKind/
                                   #   FrameDirection; SessionStatus/LeaseExpiryReason + the status sets)
    conversation_views.py  sandbox_allocation.py  sandbox_claims.py
    subscription.py  system_prompt.py  launch_identity.py  setup_output.py

  channels/            # how a messaging service holds a copy
    matrix/

  harnesses/           # harness *selection*, NOT projection — the only harness-specific code that stays
                       #   console-side after #4667. The native client + frame projection move runner-ward
                       #   into haku/runner (x/claude_code + x/codex_app_server projection are
                       #   deletion-scheduled); the runner then emits neutral operations. Never "runtimes/"
                       #   — "runtime" is retired for the backend (§3.1).
    registry.py  catalog.py     # from x/runtime.py (RuntimeAdapter/Registry) + x/runtime_catalog.py
    kind.py                     # HarnessKind — was chat_models.RuntimeKind (§3.1; stored+wire rename, not free)

  frontend/  docs/  plans/

haku/indexer/          # NEW tree (#4887, forthcoming) — the maintenance worker leaves console entirely
    recall_index_sync.py  (+ the #4872 chunk/embed split)  chat_corpus/  model_key contract test
```

House rules that bound the shape (#4924, <../../../STYLE.md> § General): no grab-bag modules
(`core.py`/`utils.py` banned), flat-over-nested (a `<3`-file domain gets no subdir — `hostexecd/`
is the landed example: a flat service.py/models.py pair, no nesting), one `py_library` per file with
gazelle-managed BUILDs (every move is mechanical), import-from-defining-module.

## 3. Canonical terminology

One concept → one name across every representation, with representation-role suffixes (`…Row`,
`…Body`, `…View`, `…Record`) **only** where two representations must coexist in one namespace, and
only **on the definition** (never re-minted per import). The verb vocabulary aligns to the
neutral-operation protocol (#4667): **opened / segment / completed** for items, a turn **ended**,
and a required **`failure`** string. The neutral-operation protocol is the runner→console wire in
the runner bridge package (`haku/runner/`, framing today in
<../../runner/protocol.py>; the concrete `neutral_operations.py` vocabulary lands with the
#4667 cutover). The console-side native-projector fold (`x/conversation_events.py`) is
deletion-scheduled by that cutover, so it is out of rename scope — new names align to the neutral
protocol, not to the fold.

### 3.1 Conversation / session vocabulary

| Concept               | Canonical name                                                        | Pydantic (wire/body)                                                  | ORM / table                                         | Notes                                                                                                                                                                                                                                                                                                                    |
| --------------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| conversation event    | one vocabulary                                                        | `conversation/conversation_event.py`                                  | `ConversationEventRow` / `conversation_event`       | merges the dataclass fold + `*Body`; discriminator kept, provenance promoted to columns                                                                                                                                                                                                                                  |
| item opened           | `…Opened` (not `…Started`)                                            | `ToolCallOpened`, `MessageOpened`, `ReasoningOpened`                  | `ConversationItem` / `conversation_item`            | drop `ITEM_STARTED`/`*Started`/`*StartedBody` — the wire says _opened_                                                                                                                                                                                                                                                   |
| item segment          | `ItemSegment`                                                         | `ItemSegment`                                                         | —                                                   | unchanged                                                                                                                                                                                                                                                                                                                |
| item completed        | `…Completed`                                                          | `ToolCallCompleted`, `MessageCompleted`, `ReasoningCompleted`         | —                                                   |                                                                                                                                                                                                                                                                                                                          |
| turn end              | `TurnEnded` + `TurnEnd` = `TurnAnswered \| TurnAborted \| TurnFailed` | one family, discriminator `outcome`                                   | `ConversationTurn` / `conversation_turn`            | drop `TurnCompleted`; drop the 3× redeclared `TurnEnd`                                                                                                                                                                                                                                                                   |
| failure reason        | field **`failure`**                                                   | `TurnFailed.failure`                                                  |                                                     | never `reason` (the one-string-three-hops unified with the #4667 cutover)                                                                                                                                                                                                                                                |
| tool outcome          | `ToolOutcome`                                                         | `ToolOutcome`                                                         |                                                     | delete the duplicate `conversation_reads.Outcome`                                                                                                                                                                                                                                                                        |
| item read model       | `Item` (or `…Item`, plain on wire)                                    | `conversation/item_reads.py` — private to the conversation read tools |                                                     | **"entry" leaves the vocabulary** (C6); the `item_entries.py` module name is already gone                                                                                                                                                                                                                                |
| session (runner life) | `Session`                                                             | `SessionRecord` (MCP) · `SessionView` (REST)                          | `Session` / `sessions`                              | three representations, one concept-name + role suffix — landed: `SessionView` is the one REST session shape                                                                                                                                                                                                              |
| channel copy          | `ChannelAttachment`                                                   | `ChannelAttachment`                                                   | `ChannelAttachment` / `channel_attachment`          | landed (C4b)                                                                                                                                                                                                                                                                                                             |
| harness wire frame    | `SessionFrame`                                                        | `HarnessFrameRecord` / `…View`                                        | `SessionFrame` / `session_frames`                   | `session/session_frames.py` holds the frame vocabulary (`BridgeFrameKind`/`FrameDirection`); `x/session_events.py` (which held `conversation_event` bodies, not frames) dissolved into `conversation/conversation_event.py` (C5)                                                                                         |
| front-end kind        | `ChannelSurface` (not `ChatSurface`)                                  |                                                                       | text col + CHECK                                    | landed (C4b)                                                                                                                                                                                                                                                                                                             |
| harness (backend)     | `harness` — retire "runtime" for the backend                          | —                                                                     | —                                                   | Claude Code / Codex. Native client + frame projection move **runner-ward** into `haku/runner` (#4667); the console residue is `harnesses/` (selection/registration). "runtime" survives only for a running incarnation (session/conversation), never the backend                                                         |
| harness kind (wire)   | `harness_kind` / `HarnessKind` (was `runtime_kind` / `RuntimeKind`)   | `HarnessKind` enum                                                    | `harness_kind` col + published `HarnessKind` schema | **rename, not free** — #4431 made `runtime_kind` a closed, read-only, **published** wire discriminator (`claude_code`\|`codex_app_server`) with a schema contract test; the rename is a coordinated stored + wire + OpenAPI change (expand/contract; published-schema consumers move in lockstep), not a mechanical swap |

**The module-name inversion** (#4772 core) is fixed: the `conversation_event` row bodies that
`x/session_events.py` held live in `conversation/conversation_event.py` (C5), and
`session/session_frames.py` is free to mean the actual session wire — `session_frames`/`SessionFrame`.

**"Entry" and "chat" leave the vocabulary.** Nothing is a _chat_ — the layers are sessions,
conversations, channels, frames, items. The code side is done (`ChannelSurface`,
`channel_attachment`, the `session_store` param); the `chat_layers.md`/`chat_runtime_facts.md`
docs still rename to the layer word they mean, and `chat_models.py` has none — a grab-bag
spanning every layer, it is scattered enum-by-enum and deleted rather than renamed (§6). An
_entry_ (`*Entry` in `conversation/reads.py`, folded by `conversation/item_reads.py`) is a third
name for the item concept beside the row and the neutral op; C6 renames it to the item read
model, staying private to the conversation read surface (beside the store/reader that produce
it), never a generic `mcp/` file.

### 3.2 Identity — the five roles (#4836, plan of record)

The families map onto **five** roles, not three. The composition (part 1) stops the actor
re-spelling the request principal's fields; parts 2-4 reshape the execution caller onto the same
atom, slim the execution context, and dissolve `ResolvedAgentBearer`. No schema or wire change.

| Role                      | Canonical name                                                                                                                                     | Home                                                            | Representation                   | Verdict                                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Authentication context    | **`<auth-context>`** family (name pending); `AgentActor` = `principal` + `operator_id` + `binding_id` + `access_profile_id`; operator arm parallel | `identity/authentication_context.py`                            | dataclass                        | **compose** (part 1)                                                                                         |
| Request principal         | `RequestPrincipal` (`agent_id` + `session_id`); profile beside it                                                                                  | `identity/request_principal.py`                                 | Pydantic                         | stays the atom; `from_source` deletes when the actor composes                                                |
| Grant principal           | `GrantPrincipal` = `AgentGrantPrincipal \| SessionGrantPrincipal` (+ future `ProfileGrantPrincipal`)                                               | `grants/principal.py`                                           | Pydantic + `principal_*` columns | stays — durable stored selector                                                                              |
| Submitter provenance      | `McpToolCallPrincipal` (+ `_ResolvedToolCallPrincipal`, `ToolCallCaller` attribution)                                                              | `database_schema.py` / `haku/shared/haku/console/tool_calls.py` | ORM + wire                       | **keep** (docstring only, no migration) — a rename is a table+trigger migration for zero behavior            |
| Runtime actor / execution | `McpExecutionCaller` = `Agent… \| Operator…`; `McpExecutionContext` slimmed to `caller` + `tool_call_id`                                           | `mcp/execution.py`                                              | Pydantic                         | **reshape** onto the principal atom (parts 2-3); `ResolvedAgentBearer` dissolves to a derivation fn (part 4) |

The one-sentence boundary the identity work inscribes (into <agent_authority.md> and the two module
docstrings): an **actor** is a **request principal** plus the accountability identities (owning
Operator, exact credential binding) that authorization and audit read and applicability must not;
**grant principals** are stored selectors those request principals are tested against; **tool-call
principal rows** are the durable submitter provenance both are revalidated from. The five-role
names graduate into <agent_authority.md>'s boundary section, not a separate doc.

### 3.3 Grant vocabulary (#4838)

| Concept              | Canonical name                                                                                       | Notes                                                                                                                                                                                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grant (domain / row) | in-package `Grant` / `KubernetesGrantRow` (`kubernetes_grants`); in-package `Grant` / `HttpGrantRow` | the in-package entity dropped to `Grant`/`GrantSpec` in both domains (C12, module-qualified where they coexist — `kubernetes.Grant` vs `http.Grant`); the `…Row` classes keep the concept-name and stay at their central `database_schema.py` definitions |
| Principal axis       | `GrantPrincipalKind` {`AGENT`, `SESSION`, future `PROFILE`}                                          | the Agent-facing `applies_to` stays `{agent, session}` **permanently**; profile principals are operator-initiated only, never Agent-requestable (#4838's load-bearing asymmetry)                                                                          |
| Lifetime axis        | temporary \| permanent                                                                               | reject `permanent × session` and click-approved `permanent × profile` by construction                                                                                                                                                                     |
| Lifecycle vocabulary | `GrantStatus` {`ACTIVE`, `RELEASED`, `REVOKED`, `EXPIRED`} + `derive_status`                         | one envelope enum (was per-domain `KubernetesGrantStatus`/`HttpGrantStatus`); status is derived from the end facts in both domains via `derive_status`, never stored                                                                                      |
| Shared envelope      | `grants/envelope.py` + `grants/provenance.py` (#4889, landed)                                        | principal columns, validity window, lifecycle owner, manual-approval source-ToolCall provenance, and the end facts (with their shared fact-shape CHECK, #4883) consolidated across both domains                                                           |

### 3.4 De-Haku (#4865) — `Haku` → `<platform>`

`<platform>` renames: the `haku/console` tree, `cluster/k8s/haku/*` (console, mailbox, managed-agent,
runtime, ui, workloads, workspaces, rbac), the `haku-*` namespaces/resources and
`haku-{console,sandbox,state}` deployments, the Haku MCP connector, `_HAKU_EXECUTION_META_KEY`, and
docs. **"Haku" survives only as one configured agent/tenant.** Tool-server ids that embed it
(`haku_index`, `haku_conversations`, `haku_routine`) fold into the #4918 rename pass. External refs
(image names, published URLs) need a redirect/compat story.

### 3.5 Tool-server names (#4918)

**Landed (C12):** one `grants` server exposes the shared grant verbs
(`create_grant`/`list_grants`/`get_grant`/`revoke_grants`) over #4889's envelope
with a `domain` discriminator (`kubernetes` | `http`) on each per-domain capability payload; the
kubernetes SAR check rides the same server as `kubernetes_can_i`, not a separate `kubernetes`
server. The grants entity-prefix drop (`KubernetesGrant` → `Grant`, `HttpGrantSpec` → `GrantSpec`,
the route wrapper/response models — §4.1 seam 3) landed with it. The old `http_grants` and
`kubernetes` server ids are gone from config, auto-approval policies, frontend catalogs, and docs in
one release; stored ToolCall `server_id` audit history keeps the old names (append-only, never
rewritten). **Remaining:** the accuracy audit of the non-grant in-process server names
(`gmail`/`google_calendar`/`hostexec`/`sandbox`) for under/over-promise; the `haku_*` tool-id
de-prefix rides C14 (§3.4).

## 4. Naming conventions — the reviewer checklist

Every burn-down PR is checkable against these. They are already <../../../STYLE.md> rules, made
concrete for this batch.

### 4.1 Directory-as-namespace / no redundant prefix

Once files live in a domain package, the package name **is** the namespace; the prefix comes off both
filenames **and** the entities they define. This is the operator decision that generalizes across
every package the split creates, not just `grants/`:

- **`grants/kubernetes/`**: `kubernetes_grant_models.py` → `models.py`,
  `kubernetes_authorization.py` → `authorization.py`, `kube_proxy_authorization.py` →
  `proxy_authorization.py` (landed with C11; `kubectl_passthrough_policy.py` keeps its name —
  `kubectl` names the external kubectl-passthrough MCP server, not the package). The entity drop
  (`KubernetesGrant` → `Grant`, `KubernetesGrantService` → `Service`, …) landed with C12 (seam 3).
- **`grants/http/`**: symmetric — `http_grant_service.py` → `service.py`,
  `http_decide_config.py` → `decide_config.py`, etc. (landed with C11); `HttpGrantSpec` →
  `GrantSpec` etc. landed with C12 (seam 3).
- **`conversation/`** _(files landed, with `ConversationRuntime` → `Runtime`)_: C5 landed the
  vocabulary into the package; the remaining entity drops (`ConversationEvent` → `Event`,
  `ConversationItem` → `Item`, `ConversationTurn` → `Turn`) stay pending; the ORM rows stay
  concept-named in the central `database_schema.py`.
- **`session/`** _(files landed, with `SessionStore` → `Store`)_: the `Session`-trio
  representations (`SessionRecord`/`SessionView`, §3.1) landed — `SessionView` is the one REST
  session shape.
- **`mcp/`**: `mcp_server.py` → `server.py`, `mcp_approval.py` → `approval.py`, `mcp_execution.py` →
  `execution.py`; `McpExecutionContext` → `ExecutionContext`, `McpExecutionCaller` → `ExecutionCaller`.
- **harness adapters**: the native client + `*_projection.py` move **runner-ward** into
  `haku/runner/` (#4667), where the backend-prefix drop applies (`claude_code_projection.py`
  → `projection.py`). Console keeps no `runtimes/`; the residual harness _selection_ is `harnesses/`
  (`RuntimeAdapter` → `Adapter`, `RuntimeRegistry` → `Registry`).

Three seams are handled deliberately — **do not reintroduce the prefix to dodge any of them**:

1. **Cross-package collision.** A consumer importing both `grants.kubernetes` and `grants.http` sees
   `Grant`/`GrantSpec` collide. Disambiguate by **module qualification** (`kubernetes.Grant` vs
   `http.Grant`) or an **alias-with-comment** at that one seam (STYLE permits aliases to avoid
   collisions) — never by baking the prefix back into the class name.
2. **Shared primitives are not domain-specific.** `HttpMethod`, `HttpOrigin`, the k8s
   `RequestAttributes`/SAR types are cross-cutting primitives used **outside** grants (egress decision
   models, standing policy, kube-api-proxy). They keep a clear shared home and keep meaningful names.
   The prefix-drop is for domain-specific grant entities, not cross-cutting primitives.
3. **Published schema components rename only in a coordinated wire pass.** The console OpenAPI
   document and the exported MCP tool schemas key components by Pydantic **class name** in one flat
   namespace, where module qualification cannot reach: the two domains' `Grant`/`GrantSpec`, the
   routes' wrapper/response models, and `KubernetesGrantScope` against the egress decision
   vocabulary's `GrantScope` all collide there, and pydantic degrades a collision to
   module-path-qualified component keys that the SPA's generated types then carry. So a model whose
   class name is a published component key renames with the C4d recipe — schema consumers in
   lockstep — not as a package-extraction rider. C11 moved the files and left these class names; the
   entity pass **landed with C12**: `KubernetesGrant`/`HttpGrant` → `Grant`, `…GrantSpec` →
   `GrantSpec`, `KubernetesGrantScope` → `GrantScope`, and the route wrapper/response models →
   `OperatorGrant`/`GrantListResponse`/`RevokeGrantRequest`, whose OpenAPI collisions pydantic now
   emits as module-path-qualified keys (e.g. `haku__console__grants__kubernetes__routes__OperatorGrant`)
   that `frontend/client.ts` consumes.

**Extension, gated on `<platform>` (#4865):** once the tree re-roots under `<platform>/console/`, the
`Haku`/`haku_` prefix on internal entities and tool-server names is redundant by the identical logic.
That drop lands with the final packaging chunks (§6, C14/C15), not the per-package ones.

### 4.2 One concept, one name

1. **One concept, one name** across Pydantic / ORM / table / wire. If a Pydantic `Apple` maps to an
   ORM row, that row is `Apple`/`AppleRow`, never `Carrot` in a `lol_inconsistency` table.
2. **Representation-role suffix only to disambiguate coexistence** (`…Row`, `…Body`, `…View`,
   `…Record`), the **concept half identical**, and the suffix **on the definition** — no `X as XRow`
   re-minted per import. Put `class ConversationEventRow` in `database_schema.py`, and the five
   `ConversationEvent as ConversationEventRow` import aliases (and their copied comments) delete.
3. **No third name for a concept that already has one** — "entry", "chat", a re-declared
   `Outcome`/`TurnEnd` are the smell.
4. **Verb vocabulary = the neutral-operation protocol**: items are **opened / segment / completed**;
   a turn is **ended**; the failure string is **`failure`**. No `started`, no `reason`, no
   `TurnCompleted`.

### 4.3 Module and import hygiene

5. **Module named by domain, not role** — no `chat_models.py`, no grab-bag `core.py`/`utils.py`; a
   module named `session_*` holds session facts, not conversation-log rows.
6. **Import from the defining module**, never a re-exporter.
7. **Flat over nested** — a domain with `<3` files gets no subdirectory.
8. **Rename before move** — settle the name in a quiet window, then the gazelle-managed move is
   mechanical.
9. **Atomic across the repo** — one PR updates every caller, BUILD, config, doc, manifest
   (<../AGENTS.md> § Refactoring); no transitional shims in-monorepo.
10. **Cross-roll safety survives the rename** — a decision vocabulary stays strict, narration stays
    tolerant, and a table/enum rename a live replica reads rides expand/contract or the
    conversation-drop allowance (<../README.md> § Vocabularies across a roll).

## 5. Binary → package dependency matrix

This is the credential-minimization contract, and it is why the split is worth the churn: **each
deployed binary's BUILD deps must physically be unable to reach a package it is not entitled to.** A
narrow database role or a network-only boundary that the code layout does not enforce is one careless
import from being violated silently; domain packages make the exclusion a build-time fact. Every
package split below keeps these exclusions structurally enforceable.

| Binary                  | Tree                                                             | May import                                                                                                                                                           | MUST NOT import                                                                                                                                                                                                                                                                                                  | Reaches console via                        |
| ----------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| **runner**              | `haku/runner`                                                    | its own bridge + shared wire                                                                                                                                         | **console (any package)** — the import arrow is console→runner, never the reverse                                                                                                                                                                                                                                | the neutral-operation socket (#4667)       |
| **haku-console server** | `<platform>/console/*`                                           | its own domain packages; the runner's shared wire vocab                                                                                                              | —                                                                                                                                                                                                                                                                                                                | in-process                                 |
| **egress proxy**        | `haku/egress` (forthcoming; egress-grant work #4941/#4957/#4942) | the shared decision wire vocabulary only                                                                                                                             | console `identity/`, `grants/`, `mcp/` — anything beyond the shared decision vocab                                                                                                                                                                                                                               | HTTP to console (not a Python import)      |
| **indexer**             | `haku/indexer/` (forthcoming, #4887)                             | the shared `haku/recall_index/` schema only                                                                                                                          | any console package — `identity/`, `grants/`, `mcp/` (incl. the `mcp/tools/recall/` read path), approval; its DB role is narrow, the deps must mirror it                                                                                                                                                         | its narrow DB role                         |
| **matrix adapter**      | `channels/matrix/` (worker.py, #4864)                            | its channel package; the conversation seam (the one pub/sub, positional read, offer-input); the schema; the narrow launch authority (`identity/launch_authority.py`) | `mcp/` and the identity auth stack (`identity/`'s `fastmcp_adapter`, `enrollment`, `authorization`, `agent_bearer_authority`, `mcp_agent_auth` — everything under `identity/` beyond `launch_authority` and the operator-identity leaves), approval, oauth, push; its DB role is narrow, the deps must mirror it | its narrow DB role                         |
| **kube-api-proxy**      | `haku/kube_api_proxy` (Go)                                       | —                                                                                                                                                                    | zero console Python (it is Go)                                                                                                                                                                                                                                                                                   | —                                          |
| **hostexecd**           | `haku/hostexec/hostexecd` (Rust, host-side)                      | —                                                                                                                                                                    | zero console Python (it is Rust)                                                                                                                                                                                                                                                                                 | HTTP to console's `hostexecd/` coordinator |

Directionality notes, verified against `devel`:

- **console → runner, never the reverse.** No file under `haku/runner` imports `haku.console`;
  console's adapters import the runner's protocol. The runner emitting neutral operations must never
  gain a console dependency — that is the whole point of #4667's boundary.
- **egress depends on the shared decision vocab, not on console.** The egress proxy reaches console
  only over HTTP and shares a small decision wire vocabulary; console may depend on that shared vocab,
  the egress binary must not depend on console. Its BUILD deps cannot reach `identity/`/`grants/`/`mcp/`.
- **the indexer's narrow DB role is mirrored in the dep graph.** Console keeps the query-time read
  path (folded under `mcp/tools/recall/`, since its sole consumer is the `haku_index` MCP server); the
  worker (`recall_index_sync.py`) leaves to `haku/indexer/` and is entitled to the shared
  `haku/recall_index/` schema and its own code — not to any console package.
- **the two host-side daemons carry zero console Python** by language (Go, Rust); their only coupling
  is the HTTP surface, and `haku/console/hostexecd/` is the console-side coordinator for the Rust
  `haku/hostexec/hostexecd/` — one concept, one name, two locations.
- **one binary, one config.** A binary's config surface mirrors its entitlement column the way its
  BUILD deps do: `ConsoleConfigFile` is the console server's alone; the indexer parses only its
  narrow `IndexerConfigFile` slice of the shared YAML (§6 C13, the first instance); the egress proxy
  is env-only by design; the runner takes its launch config over the bridge. No binary parses
  another's config model.

## 6. Burn-down strategy

The organizing rule (#4924): **each package materializes when its domain is being restructured
anyway** — moving files twice is the waste to avoid, so a rename rides a consolidation already in
flight, and the batch never becomes a stop-the-world. Every chunk below is one independently-approvable
change-unit, split by change and dispatched in parallel where the domains do not touch. **Mechanical**
= a quiet-window rename/move needing no design review; **semantic** = a reshape that needs review. The
scarce resource is operator review, so ready mechanical work never queues behind a contested reshape.

**Four lanes** run in parallel — indexer, identity, grants, conversation — plus the trailing
de-Haku/packaging sweep. The only real waits are **content** dependencies (a
blessed `<platform>` name, the `<auth-context>` pick, #4889's envelope shape, #4667's deletion). "It
will conflict" / "touches the same file" is not a wait — whoever lands second rebases.

### Indexer lane — independent of all naming work

- **C13 · `haku/indexer/` tree move (#4887)** _(staged; the directory move is the last step)_ — the
  worker (`indexer.py` + `recall_index_sync.py`) leaves console; console keeps the query-time read
  path (`mcp/tools/recall/`). Not a bare rename: the §5 exclusion — worker deps physically unable to
  reach console — must already hold when the tree appears, so the console edges sever in place first,
  each step an atomic sweep with no shims, beside every other lane. The shared recall-index/embedder
  config lives in `haku/recall_index/config.py` and the worker reads only its own
  `IndexerConfigFile` slice of the shared YAML; remaining, in order:
  1. a shared home for `ConversationItem` and the item enums (**operator decision, pending** — the
     gating semantic piece): `haku/recall_index/chat_source.py` imports
     `haku.console.{chat_models,database_schema}` today, the §5 violation this step removes;
  2. the mechanical move, retargeting `devinfra/ci/image_targets.json` and BUILD — the GHCR name
     `haku-indexer` keys off the JSON key, not the Bazel path, so Flux image automation is untouched.

### Conversation / session lane — the #4772 core

The #4667 stage-4 cutover landed (#4984), and the package materialization rode it: the runtime
graduated out of `x/` into `conversation/`, `session/`, and the wakes into `notifications/`,
with the §4.1 file de-prefix and the `SessionStore` → `Store` / `ConversationRuntime` →
`Runtime` entity drops. What remains is the vocabulary work below, in a conversation-domain
quiet gap.

- **C4 · `chat_models.py` dissolution** — the de-"chat" renames around it (C4a `chat_store` →
  `session_store`, C4b `ChannelSurface` + `channel_attachment`) have landed. The module itself is
  a transitional grab-bag whose enums span every layer, so it has no single layer word and is
  **deleted** rather than renamed. Its survival _is_ the tracked cleanup item — the #4772 reorg
  is not done until it is gone, each enum scattered to its true home. Landed:
  `SessionStatus`/`LeaseExpiryReason` + the status frozensets in `session/status.py`,
  `BridgeFrameKind`/`FrameDirection` in `session/session_frames.py`, the prompt-origin models
  (`SpaOrigin`/`MatrixOrigin`/`HarnessOrigin`/`PromptOriginKind`) in `conversation/prompt_origin.py`,
  and `PromptRejection` in `conversation/conversation_event.py` (the `PromptRejected` body's
  discriminant; `prompt_inbox.py` reads `database_schema`, which reads the event vocabulary, so the
  refusal enum lands with the event rather than the inbox). Remaining:
  - `ItemType`, `ItemStatus`, `ToolOutcome` → `conversation/item_reads.py` (C6) — blocked until then:
    `database_schema.py` reads them for its columns while `item_reads.py` imports the ORM rows, so
    moving them today would import-cycle; C6 owes them a leaf home
  - `ChannelSurface` → `channels/` with the channel packaging
- **C4d · `runtime_kind` → `harness_kind`** _(semantic — coordinated stored + wire + OpenAPI)_ — the
  harness-kind discriminator (§3.1). Landed: the wire-field drop (#5050); the `RuntimeKind` enum →
  `harnesses/kind.py` as `HarnessKind` and its OpenAPI component (SPA consumers read the
  `harness_kind` field and its unchanged `claude_code`/`codex_app_server` values, not the component
  name, so the key flip stays compatible across the independent static/API rolls); and the **expand**
  step of the stored `conversation.runtime_kind` → `harness_kind` column — `harness_kind` added,
  backfilled and dual-written while reads stay on `runtime_kind`, so a post-#5050 replica never reads
  a vanished column mid-roll. Remaining is the stored **contract**, each release only after the prior
  has converged: switch reads to `harness_kind` (backfill the roll-window stragglers, add NOT NULL);
  stop writing and mapping `runtime_kind` (make it nullable); drop `runtime_kind` and its CHECK. The
  console harness adapters (`x/claude_code`, `x/codex_app_server`) are deleted by the #4667 cutover
  (native projection moves runner-ward), so there is no console `runtimes/`→`harnesses/` move to
  schedule — only this discriminator rename and the small `harnesses/` selection residue.
- **C4e · `allowed_chat_runtimes` → `allowed_harnesses`** _(semantic — per-profile config field,
  deploy-coordinated expand/contract, same recipe as the `chat_runtimes` → `harnesses` key flip)_.
  **Expand landed**: `allowed_harnesses` is the canonical per-profile field, its readers (console
  `app.py`, the harness/profile cross-check in `mcp_config.py`, the Matrix adapter worker) switched,
  and both parsers of the shared YAML — `AccessProfile` (console, `extra="forbid"`) and
  `ConfiguredProfile` (Matrix adapter) — accept the deployed `allowed_chat_runtimes` key as a
  tombstoned alias and reject a profile setting both. The ConfigMap deliberately still writes
  `allowed_chat_runtimes` until an image with these loaders is rolled out. **Remaining is the
  contract**, after the expand has converged: flip the ConfigMap per-profile key to
  `allowed_harnesses` and drop both twin aliases.
- **C6 · "entry" → item read model** _(semantic)_ — `*Entry` → `Item…`, into `conversation/item_reads.py`
  (private to the conversation read surface, beside the store that produces it — not `mcp/`).

### Identity lane — rides the #4836 compose PR

Needs operator go **and** the `<auth-context>` name pick.

- **C8 · Compose authentication-context (#4836 parts 1-4)** _(semantic, ~30 files)_ — compose the
  actor, reshape the execution caller onto the principal atom, slim `McpExecutionContext`, dissolve
  `ResolvedAgentBearer`. No schema/wire change.
- **C10 · `identity/` package extraction** _(mechanical)_ — the package fold landed independently of
  C8: the flat `operator_*` modules, `agent_bearer_authority.py`, `mcp_agent_auth.py`, the `agents/`
  package (`agents/models.py` → `identity/agent.py`), and `mcp_auth/fastmcp_adapter.py` moved into
  `identity/`, each a one-file `py_library` so `channels/matrix` still deps only
  `identity/launch_authority` (§5). Still riding the #4836 vocabulary, after C8 settles the names: the
  `grants/principal.py` split (`RequestPrincipal` → `identity/request_principal.py`; the
  `GrantPrincipal` family + `applies_to` stay), the `tool_call_actor.py` → `authentication_context.py`
  rename, and the `ResolvedAgentBearer` dissolution.

### Grants lane

- **#4918 roster audit** _(mechanical)_ — the tool-server consolidation landed (C12: one `grants`
  server over #4889's envelope with a `domain` discriminator, `can_i` folded in as
  `kubernetes_can_i`, and the grants entity-prefix drop — §3.5, §4.1 seam 3 — in one coordinated
  release across config, auto-approval policies, frontend catalogs, and docs; stored ToolCall
  `server_id` audit history left as-is). Remaining: audit the non-grant in-process server names
  (`gmail`/`google_calendar`/`hostexec`/`sandbox`) for under/over-promise; the `haku_*` tool-id
  de-prefix rides C14.

### De-Haku and final packaging — last

- **C14 · `<platform>` rename (#4865)** _(semantic — largest, most cross-cutting)_ — deliberately last,
  so it moves settled names once. Needs the blessed `<platform>` name; repo tree + cluster manifests +
  namespaces + connector + docs in a coordinated cutover; "Haku" kept as agent config; redirect/compat
  for external refs. The tool-id de-Haku rides C12.
- **C15 · Remainder packaging (#4924)** _(mechanical)_ — Landed: `oauth/` (#5000),
  `notifications/` (#5001), `hostexecd/` (#5002) carved off the flat top level. Remaining: the rest
  settles around the app shell once #4772 has settled what everything is called
  (rename-before-move), in a final quiet-window sweep.

### Dependency-ordered picture

```text
now ─┬─ C13               ← staged severing; ConversationItem home is the gate   [indexer lane, independent]
     ├─ C8 → C10          ← after operator go + <auth-context>  [identity lane]
     ├─ #4918 audit       ← C12 landed (one `grants` server)    [grants lane]
     └─ C4e → C6                            ← packages + C5 landed         [conversation lane]
                  │
                  C14 (de-Haku) ────────────────→ after lanes settle names
                  C15 (final packaging) ────────→ last
```

The two remaining contested reshapes (C8, C14) each have uncontested siblings that land ahead of
them, so no ready work ever queues behind a rename argument.
