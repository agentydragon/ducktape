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
    request_principal.py          # the RequestPrincipal atom, split out of grant_principal.py

  grants/              # shared-envelope grant model — materializes with #4889
    principal.py       # GrantPrincipal family + applies_to (rest of grant_principal.py)
    envelope.py        # shared ownership/approval/lifetime/audit/binding envelope (#4889, forthcoming)
    kubernetes/        # from kubernetes_grant_{models,repository,service,routes}, kubernetes_authorization,
                       #   kubectl_passthrough_policy, kube_proxy_authorization
    http/              # from http_grant_{models,repository,service,routes}, http_decide_{config,routes,service}

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

  conversation/        # the durable, provider-neutral record (landed except conversation_event.py)
    conversation_event.py          # ONE Pydantic vocabulary: row body + MCP/SPA wire — still x/-side
                                    #   (merges x/conversation_events.py + x/session_events.py)  [#4772 core, C5]
    reads.py  reader.py  log.py  follow.py  history.py  live_updates.py  runtime.py  reprojection.py
    item_reads.py                  # folded item read models ("entry" leaves with C6) — private to the
                                   #   conversation read surface (the store/reader that produce them + the
                                   #   haku_conversations tool that serves them); NOT a generic mcp/ file (§4.2)
    prompt_inbox.py  journal_consumer.py    # the durable prompt inbox + the #4667 journal commit

  session/             # one runner incarnation + its wire log (landed except session_frames.py)
    store.py  runtime.py
    session_frames.py              # forthcoming with the inversion fix (§3): C5 moving the conversation
                                    #   bodies out of x/session_events.py frees the name for the wire log
    conversation_views.py  sandbox_allocation.py  sandbox_claims.py
    subscription.py  system_prompt.py  launch_identity.py  setup_output.py

  channels/            # how a messaging service holds a copy
    matrix/

  harnesses/           # harness *selection*, NOT projection — the only harness-specific code that stays
                       #   console-side after #4667. The native client + frame projection move runner-ward
                       #   into haku/runtime/x/bridge (x/claude_code + x/codex_app_server projection are
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
the runner bridge package (`haku/runtime/x/bridge/`, framing today in
<../../runtime/x/bridge/protocol.py>; the concrete `neutral_operations.py` vocabulary lands with the
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
| session (runner life) | `Session`                                                             | `SessionRecord` (MCP) · `SessionView` (REST)                          | `Session` / `sessions`                              | three representations, one concept-name + role suffix; fix the "…agent conversation" table docstring                                                                                                                                                                                                                     |
| channel copy          | `ChannelAttachment`                                                   | `ChannelAttachment`                                                   | `ChannelAttachment` / `channel_attachment`          | landed (C4b)                                                                                                                                                                                                                                                                                                             |
| harness wire frame    | `SessionFrame`                                                        | `HarnessFrameRecord` / `…View`                                        | `SessionFrame` / `session_frames`                   | `x/session_events.py` → `session/session_frames.py` (the inversion fix — it holds `conversation_event` bodies today, not frames)                                                                                                                                                                                         |
| front-end kind        | `ChannelSurface` (not `ChatSurface`)                                  |                                                                       | text col + CHECK                                    | landed (C4b)                                                                                                                                                                                                                                                                                                             |
| harness (backend)     | `harness` — retire "runtime" for the backend                          | —                                                                     | —                                                   | Claude Code / Codex. Native client + frame projection move **runner-ward** into `haku/runtime/x/bridge` (#4667); the console residue is `harnesses/` (selection/registration). "runtime" survives only for a running incarnation (session/conversation), never the backend                                               |
| harness kind (wire)   | `harness_kind` / `HarnessKind` (was `runtime_kind` / `RuntimeKind`)   | `HarnessKind` enum                                                    | `harness_kind` col + published `HarnessKind` schema | **rename, not free** — #4431 made `runtime_kind` a closed, read-only, **published** wire discriminator (`claude_code`\|`codex_app_server`) with a schema contract test; the rename is a coordinated stored + wire + OpenAPI change (expand/contract; published-schema consumers move in lockstep), not a mechanical swap |

**The module-name inversion** (#4772 core): `x/session_events.py` holds `conversation_event` _row
bodies_, not session frames — the actual session wire is `session_frames`/`SessionFrame`. Its own
first line calls itself "the stream's two categories as `conversation_event` rows". The fix moves the
conversation bodies into `conversation/conversation_event.py` and frees `session/session_frames.py`
to mean what it says.

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
| Authentication context    | **`<auth-context>`** family (name pending); `AgentActor` = `principal` + `operator_id` + `binding_id` + `access_profile_id`; operator arm parallel | `identity/authentication_context.py`                            | dataclass                        | **compose** (part 1); `ToolCallActor` union → optionally `RuntimeActor`                                      |
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

| Concept              | Canonical name                                                                               | Notes                                                                                                                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grant (domain / row) | `KubernetesGrant` / `KubernetesGrantRow` (`kubernetes_grants`); `HttpGrant` / `HttpGrantRow` | the `…Row`-at-definition pattern is already right on the K8s side — mirror it for HTTP. Inside `grants/{kubernetes,http}/` the prefix drops to `Grant`/`GrantRow` (§4.1)         |
| Principal axis       | `GrantPrincipalKind` {`AGENT`, `SESSION`, future `PROFILE`}                                  | the Agent-facing `applies_to` stays `{agent, session}` **permanently**; profile principals are operator-initiated only, never Agent-requestable (#4838's load-bearing asymmetry) |
| Lifetime axis        | temporary \| permanent                                                                       | reject `permanent × session` and click-approved `permanent × profile` by construction                                                                                            |
| Shared envelope      | `grants/envelope.py` (#4889, forthcoming)                                                    | ownership / approval / lifetime / audit / credential-binding — the `grants/` package materializes when this lands                                                                |

### 3.4 De-Haku (#4865) — `Haku` → `<platform>`

`<platform>` renames: the `haku/console` tree, `cluster/k8s/haku/*` (console, mailbox, managed-agent,
runtime, ui, workloads, workspaces, rbac), the `haku-*` namespaces/resources and
`haku-{console,sandbox,state}` deployments, the Haku MCP connector, `_HAKU_EXECUTION_META_KEY`, and
docs. **"Haku" survives only as one configured agent/tenant.** Tool-server ids that embed it
(`haku_index`, `haku_conversations`, `haku_routine`) fold into the #4918 rename pass. External refs
(image names, published URLs) need a redirect/compat story.

### 3.5 Tool-server names (#4918)

`kubernetes` → `kubernetes_grants` (it carries grants **and** `can_i`); audit
`gmail`/`google_calendar`/`hostexec`/`sandbox`/`http_grants` for under/over-promise; consider one
`grants` server with a domain discriminator once #4889's envelope is in hand. Coordinated cutover:
config + auto-approval policies + docs in one release; stored `server_id` audit rows keep the old
name.

## 4. Naming conventions — the reviewer checklist

Every burn-down PR is checkable against these. They are already <../../../STYLE.md> rules, made
concrete for this batch.

### 4.1 Directory-as-namespace / no redundant prefix

Once files live in a domain package, the package name **is** the namespace; the prefix comes off both
filenames **and** the entities they define. This is the operator decision that generalizes across
every package the split creates, not just `grants/`:

- **`grants/kubernetes/`**: `kubernetes_grant_models.py` → `models.py`; `KubernetesGrant` → `Grant`,
  `KubernetesGrantRow` → `GrantRow`, `KubernetesGrantService` → `Service`;
  `routes`/`authorization`/`kubectl_passthrough_policy`/`kube_proxy_authorization` lose the
  `kubernetes`/`kube` prefix.
- **`grants/http/`**: symmetric — `HttpGrantSpec` → `GrantSpec`, `http_grant_service.py` → `service.py`, etc.
- **`conversation/`** _(files landed, with `ConversationRuntime` → `Runtime`)_: the remaining
  entity drops (`ConversationEvent` → `Event`, `ConversationItem` → `Item`, `ConversationTurn` →
  `Turn`) ride the C5 vocabulary merge that brings those definitions into the package; the ORM
  rows stay concept-named in the central `database_schema.py`.
- **`session/`** _(files landed, with `SessionStore` → `Store`)_: the `Session`-trio
  representations (`SessionRecord`/`SessionView`, §3.1) are C7's to settle.
- **`mcp/`**: `mcp_server.py` → `server.py`, `mcp_approval.py` → `approval.py`, `mcp_execution.py` →
  `execution.py`; `McpExecutionContext` → `ExecutionContext`, `McpExecutionCaller` → `ExecutionCaller`.
- **harness adapters**: the native client + `*_projection.py` move **runner-ward** into
  `haku/runtime/x/bridge/` (#4667), where the backend-prefix drop applies (`claude_code_projection.py`
  → `projection.py`). Console keeps no `runtimes/`; the residual harness _selection_ is `harnesses/`
  (`RuntimeAdapter` → `Adapter`, `RuntimeRegistry` → `Registry`).

Two seams are handled deliberately — **do not reintroduce the prefix to dodge either**:

1. **Cross-package collision.** A consumer importing both `grants.kubernetes` and `grants.http` sees
   `Grant`/`GrantSpec` collide. Disambiguate by **module qualification** (`kubernetes.Grant` vs
   `http.Grant`) or an **alias-with-comment** at that one seam (STYLE permits aliases to avoid
   collisions) — never by baking the prefix back into the class name.
2. **Shared primitives are not domain-specific.** `HttpMethod`, `HttpOrigin`, the k8s
   `RequestAttributes`/SAR types are cross-cutting primitives used **outside** grants (egress decision
   models, standing policy, kube-api-proxy). They keep a clear shared home and keep meaningful names.
   The prefix-drop is for domain-specific grant entities, not cross-cutting primitives.

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

| Binary                  | Tree                                                             | May import                                                                                                                                                         | MUST NOT import                                                                                                                                          | Reaches console via                        |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| **runner**              | `haku/runtime/x/bridge`                                          | its own bridge + shared wire                                                                                                                                       | **console (any package)** — the import arrow is console→runner, never the reverse                                                                        | the neutral-operation socket (#4667)       |
| **haku-console server** | `<platform>/console/*`                                           | its own domain packages; the runner's shared wire vocab                                                                                                            | —                                                                                                                                                        | in-process                                 |
| **egress proxy**        | `haku/egress` (forthcoming; egress-grant work #4941/#4957/#4942) | the shared decision wire vocabulary only                                                                                                                           | console `identity/`, `grants/`, `mcp/` — anything beyond the shared decision vocab                                                                       | HTTP to console (not a Python import)      |
| **indexer**             | `haku/indexer/` (forthcoming, #4887)                             | the shared `haku/recall_index/` schema only                                                                                                                        | any console package — `identity/`, `grants/`, `mcp/` (incl. the `mcp/tools/recall/` read path), approval; its DB role is narrow, the deps must mirror it | its narrow DB role                         |
| **matrix adapter**      | `channels/matrix/` (worker.py, #4864)                            | its channel package; the conversation seam (the one pub/sub, positional read, offer-input); the schema; the narrow launch authority (`agents/launch_authority.py`) | `mcp/` and the auth stack (`mcp_auth/`, enrollment, bearer machinery), approval, oauth, push; its DB role is narrow, the deps must mirror it             | its narrow DB role                         |
| **kube-api-proxy**      | `haku/kube_api_proxy` (Go)                                       | —                                                                                                                                                                  | zero console Python (it is Go)                                                                                                                           | —                                          |
| **hostexecd**           | `haku/hostexec/hostexecd` (Rust, host-side)                      | —                                                                                                                                                                  | zero console Python (it is Rust)                                                                                                                         | HTTP to console's `hostexecd/` coordinator |

Directionality notes, verified against `devel`:

- **console → runner, never the reverse.** No file under `haku/runtime` imports `haku.console`;
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
  is not done until it is gone, each enum scattered to its true home:
  - `SessionStatus`, `LeaseExpiryReason` (+ the session-status frozensets) → `session/` (C7-adjacent)
  - `ConversationEventKind`, `AuthoredEventKind`, `EventProvenance`, `TurnOutcome`, `ReasoningDisclosure`
    → `conversation/conversation_event.py` (C5)
  - `ItemType`, `ItemStatus`, `ToolOutcome` → `conversation/item_reads.py` (C6)
  - `RuntimeKind` → `harnesses/kind.py` as `HarnessKind` (C4d)
  - `BridgeFrameKind`, `FrameDirection` → the session-frames home (`session/session_frames.py`)
  - the prompt-origin models (`SpaOrigin`/`MatrixOrigin`/`HarnessOrigin`/`PromptOriginKind`) and
    `PromptRejection` → the prompt vocabulary (conversation lane)
- **C4d · `runtime_kind` → `harness_kind`** _(semantic — coordinated stored + wire + OpenAPI)_ — the
  harness-kind discriminator (§3.1), mid-roll: every wire surface publishes both names and every
  reader is on `harness_kind`. Remaining: drop `runtime_kind` from the writers (wire models, their
  schema pins, the SPA mirror) once the reader-switch image is rolled out — earlier breaks a
  still-deployed reader mid-roll; then the stored `conversation.runtime_kind` column and the
  `RuntimeKind` enum / OpenAPI component → `HarnessKind`, a stored + published rename that needs its
  own expand/contract roll story, same care as C4b. The console harness adapters (`x/claude_code`,
  `x/codex_app_server`) are deleted by the #4667 cutover (native projection moves runner-ward), so
  there is no console `runtimes/`→`harnesses/` move to schedule — only this discriminator rename and
  the small `harnesses/` selection residue.
- **C4e · `allowed_chat_runtimes` → `allowed_harnesses`** _(semantic — per-profile config field,
  deploy-coordinated expand/contract, same recipe as the `chat_runtimes` → `harnesses` key flip)_.
- **C5 · One Pydantic conversation-event vocabulary** _(semantic — the contested one)_ — merge the
  fold + `*Body` into `conversation/conversation_event.py`, aligned to neutral-op names, dataclasses
  gone, `UnknownEventBody` arm preserved. Everything above lands independently of it.
- **C6 · "entry" → item read model** _(semantic)_ — `*Entry` → `Item…`, into `conversation/item_reads.py`
  (private to the conversation read surface, beside the store that produces it — not `mcp/`); rides or follows C5.
- **C7 · Session-trio dedup** _(semantic)_ — after the conversation quiet window
  (`ConversationTurnView` is already gone).

### Identity lane — rides the #4836 compose PR

Needs operator go **and** the `<auth-context>` name pick.

- **C8 · Compose authentication-context (#4836 parts 1-4)** _(semantic, ~30 files)_ — compose the
  actor, reshape the execution caller onto the principal atom, slim `McpExecutionContext`, dissolve
  `ResolvedAgentBearer`. No schema/wire change.
- **C9 · Role docstrings + `ToolCallActor` → `RuntimeActor`** _(mechanical rider)_ — the five-role
  boundary at their definitions (also the fallback deliverable if C8 is declined).
- **C10 · `identity/` package extraction** _(mechanical)_ — lands **with** the #4836 vocabulary, after
  C8 settles the names. Splits `grant_principal.py`: `RequestPrincipal` → `identity/`,
  `GrantPrincipal` + `applies_to` → `grants/`.

### Grants lane — after egress lands + #4889

- **C11 · `grants/` package extraction** _(mechanical move of a semantic consolidation)_ — materializes
  **with #4889's shared envelope**; K8s + HTTP grant modules → `grants/{kubernetes,http}/` +
  `grants/principal.py` + `grants/envelope.py`.
- **C12 · Tool-server naming (#4918)** _(semantic — coordinated cutover)_ — `kubernetes` →
  `kubernetes_grants`, roster audit, the one-`grants`-server decision with #4889's shape in hand.
  Config + policies + docs in one release.

### De-Haku and final packaging — last

- **C14 · `<platform>` rename (#4865)** _(semantic — largest, most cross-cutting)_ — deliberately last,
  so it moves settled names once. Needs the blessed `<platform>` name; repo tree + cluster manifests +
  namespaces + connector + docs in a coordinated cutover; "Haku" kept as agent config; redirect/compat
  for external refs. The tool-id de-Haku rides C12.
- **C15 · Remainder packaging (#4924)** _(mechanical)_ — once #4772 has settled what everything is
  called (rename-before-move), the remaining flat top level settles around the app shell in a
  final quiet-window sweep.

### Dependency-ordered picture

```text
now ─┬─ C13               ← staged severing; ConversationItem home is the gate   [indexer lane, independent]
     ├─ C8 → C9 → C10     ← after operator go + <auth-context>  [identity lane]
     ├─ C11 → C12         ← after egress lands + #4889          [grants lane]
     └─ C4e, C5 → C6 → C7                       ← packages landed              [conversation lane]
                    │
                    C14 (de-Haku) ──────────────→ after lanes settle names
                    C15 (final packaging) ──────→ last
```

The three contested reshapes (C5, C8, C14) each have uncontested siblings that land ahead of them, so
no ready work ever queues behind a rename argument.
