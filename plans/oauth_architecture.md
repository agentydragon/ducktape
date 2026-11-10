# OAuth and identity work across Ducktape

- **Status:** Haku Console's former MCP OAuth ingress and tool-call execution/approval surfaces are
  retired. The Console retains canonical Operator/Agent identity data and its Operator-facing
  Agent catalog; static Agent reconciliation is retired. This file tracks remaining identity and
  cross-component OAuth/security work.
- **Updated:** 2026-10-28
- **Implemented contract:**
  <../haku/console/README.md#canonical-agent-authority>
- **Tactical Haku backlog:** <../haku/console/TODO.md>

Git and the closed prototype PRs are the archive for P0-P5. The former execution diary, spike
narrative, PR ledger, proposed schema, and #3122 parts bin were removed after the terminal cutover;
they are not current plans.

## Decisions that still govern future work

Keep these ownership boundaries:

| Component         | Owns                                                                                                                                            | Does not own                                                  |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Authentik         | Human/machine authentication, IdP policy, upstream issuance and exchange                                                                        | Haku Agents, names, or application lifecycle                  |
| FastMCP           | MCP discovery, client metadata/registration, transactions, redirect/PKCE validation, callback, codes, local token families, bearer verification | Haku's Operator-authenticated product interaction             |
| FastAPI/Starlette | Route composition, application dependencies, browser sessions, CSRF, Jinja rendering                                                            | MCP authorization-server protocol state                       |
| Haku              | Canonical Operator/Agent domain, enrollment, grants/bindings, lifecycle, downstream hub, UI, and audit                                          | Generic Authentik/FastMCP infrastructure                      |
| `mcp_infra`       | Narrow typed protocol composition, direct JWT verification, credentialed facades, identity-preserving exchange                                  | Haku product policy                                           |
| Airlock           | OAuth provider connections, refresh-token custody, and access-token publication                                                                 | MCP ingress, tool execution, approval, or Haku Agent identity |

The remaining work must preserve these distinctions:

- OAuth client software and `client_id` are registration metadata, not a Haku Agent.
- Browser OIDC login, MCP Agent enrollment, and a downstream provider connection are separate
  authorization relationships.
- A principal is a canonical local ID; a binding is accepted credential evidence; a display name
  is presentation; a grant is one OAuth relationship/token family.
- Exact `(issuer, subject)` identities converge only through an explicitly configured trust-domain
  anchor. Username is never durable authority.
- Logout, silence, and `last_seen_at` are not revocation. Haku-local revoke/disable is authoritative.
- Private Postgres/Valkey/Kubernetes Secret storage is an accepted credential boundary. Additional
  application encryption is optional, not a prerequisite.

A DCR-capable IdP alone is not a reason to switch authorization servers: DCR registers software and
does not supply Haku's Agent ceremony or lifecycle.

## Remaining plan

### R0: make independent console rollouts skew-safe

Do this before the next change that couples server code, runtime config schema, and static frontend.
The 2026-07-14 cutover demonstrated the gap: Flux applied the new `static_agents` config before the
matching server image was published, so the old image crash-looped until image automation caught
up. `Recreate` turned that temporary skew into full unavailability.

Keep the server image, static image, and live config independently deployable. Evolve their
contracts over one rollout window: readers before writers for config, server API additions before
frontend consumers, and removals only after every consumer has moved. CI should exercise the
server against its current and next config shapes and the frontend against every supported server
contract. Revisit `Recreate` so a failed replacement leaves the last serving version available.

Acceptance:

- every intermediate server/static/config combination within the supported rollout window works;
- CI rejects a writer or frontend that requires a contract not yet served;
- contract removal is gated on the last old consumer leaving service; and
- a failed rollout leaves the last serving version available rather than requiring image
  automation to repair an outage.

### C1: simplify the authority schema after Connected Agents

The current `0135` schema baseline retains 33 `haku_0009_*` PostgreSQL functions alongside the
transactional authority. Simplify the live identity entities after H1 establishes the Connected
Agents read contract and shows which joins are genuine friction, but before H2 lifecycle mutations
make the current graph a product dependency. Migration `0138` retired static Agents; their retained
rows are history, not active reconciliation input. Historical tool-call tables and rows are also
retained and are outside this schema cleanup. Delete only triggers made unnecessary by changes to
the live identity graph. Retain ordinary `NOT NULL`/`CHECK`/unique/FK constraints, the
one-active-binding index, same-Agent predecessor integrity, and genuinely cross-row security rules.

Recommended terminal shape:

- Put required `display_name` and normalized unique key on `Agent`. Allow creates the draft Agent;
  exchange still proves the upstream principal belongs to the same Operator before issuing a
  binding. Remove the deferred `AgentNameReservation` ownership cycle. Add a small rename-audit
  table later only if name history is a product requirement; do not permanently reserve retired
  names by default.
- Let FastMCP own registration. Replace Haku's speculative `ClientSoftware` mirror with only the
  immutable client ID and optional display-name snapshot needed by the enrollment interaction and
  grant. Do not store unprovable DCR/CIMD provenance, a write-only metadata hash, or an always-null
  icon.
- Keep Agent, credential binding, and grant as separate entities. Reconsider redundant Agent status
  transitions only when disable/delete semantics are specified; do not derive away a future
  Agent-level policy control prematurely.

This is not a five-second-stamp PR. Stage it after H1, with one schema migration and focused
invariant tests, before H2 makes lifecycle contracts depend on the current graph.

### Haku product sequence

These are vertical product PRs, not another identity migration:

| Order | Slice                           | Boundary and acceptance                                                                                                                                                                                                                                                                              |
| ----- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| H1    | Connected Agents                | Operator-scoped API and trusted-console UI together. Derive name, client metadata, scopes, binding status/generation, timestamps, and reconnect history through canonical joins. Do not call inactivity “disconnected” or claim DCR/CIMD provenance that current public FastMCP inputs cannot prove. |
| H2a   | Revoke and disable              | Operator-owned API, UI, audit representation, and negative tests. Distinguish revoking one credential binding/grant from disabling the Agent and all usable bindings; keep the public control scoped to the Agent rather than an internal grant identifier.                                          |
| H2b   | Rename                          | Atomically replace the Agent's required normalized, globally unique name. Add append-only rename audit only if the product needs history; retired names need not remain authority-bearing reservations.                                                                                              |
| H2c   | Tombstone and reconnect history | Add explicit delete/tombstone semantics and cleanup/history UI after revoke/disable and rename semantics are stable.                                                                                                                                                                                 |

### Independent security and consolidation lanes

These do not block H1-H2:

| Lane                             | Work                                                                                                                                                                                                                       |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1: typed auth configuration     | Replace optional-heavy incoming/outgoing auth config with role-specific discriminated models and typed scope domains, atomically at each consumer. Keep credentialed-facade and identity-delegation constructors separate. |
| S2: browser OIDC helper          | Extract only genuinely shared Authlib/Starlette relying-party behavior from Haku and Props; migrate Study Casino from username authority to local UUID plus exact `(issuer, subject)`.                                     |
| I1: singular Authentik ownership | Inventory remaining provider/application/controller ownership, assign shared mappings one owner, update `<../cluster/docs/mcp_oauth_authentik_notes.md>` for preregistration/CIMD/DCR preference, and add drift checks.    |
| D1: public-client abuse controls | Add Haku-side enrollment/registration rate limits and transaction quotas if public DCR remains enabled. FastMCP retains redirect/CIMD mechanics and protocol TTLs; Haku retains interaction/activation expiry.             |

Retiring Airlock's remaining OAuth grants is a separate credential-migration program. Its removed
MCP proxy and approval queue must not be revived as part of that migration. The retired
`x/agent_server` experiment remains design archaeology in Git history, not an implementation base
or cleanup prerequisite.

## Future-change guardrails

| Avoid                                                                     | Preserve instead                                                                    |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Agent creation in DCR `/register`                                         | Operator-authenticated Haku enrollment after FastMCP validates authorization inputs |
| Agent keyed by `client_id`, username, display name, or token JTI          | Local UUID Agent plus explicit grant/binding evidence                               |
| Equal bare `sub` values treated as global identity                        | Exact identities linked only through configured trust-domain anchors                |
| Optional-heavy caller/auth shapes and string discriminator dispatch       | Enums and discriminated unions narrowed with `isinstance` or `match`                |
| Product HTML in generic auth infrastructure or Python strings             | Haku-owned Jinja templates with autoescape and structured values                    |
| Parent FastAPI dependency assumed to secure mounted FastMCP routes        | FastMCP provider/middleware for `/mcp`; explicit dependencies for parent routes     |
| Transient storage/network failures translated to `401` or `invalid_grant` | Classified retryable service failures without mutating grant state                  |
| Logout, silence, or last-seen treated as revocation                       | Explicit local grant/Agent lifecycle checked on access and refresh                  |
| Generic `oidc_proxy_factory` or scattered private FastMCP access          | Explicit Haku adapter with one pinned compatibility module                          |
| One universal auth service or untyped scope list                          | Small libraries and separate MCP, identity, and backend-delegation scope domains    |

## Acceptance gates

- Every new Agent read, lifecycle route, event, or cache begins with
  canonical Operator ownership and includes Agent/binding scope where applicable.
- Every lifecycle operation revalidates the current Agent and binding state at commit time; a
  stale or superseded binding cannot authorize the mutation.
- UI metadata comes from canonical joins and is treated as untrusted presentation. Secrets and raw
  OAuth material never enter API models, logs, traces, or test artifacts.
- Release automation validates and promotes one server/static/config tuple.
- Airlock changes prove credentials work only on intended route surfaces and anonymous provider
  initiation fails.
