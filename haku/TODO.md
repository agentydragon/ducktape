# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done. Component-specific detail stays in the
component checklist rather than being copied here: haku-console's tool/API backlog is
<console/TODO.md>, while the remaining OAuth/identity rationale and sequencing is
<../plans/oauth_architecture.md>.

## Immediate correctness and security

- **Make the deployed haku-console release tuple atomic.** Promote server image, static image, and
  live config revision/schema as one CI-tested unit. The 2026-07-14 authority cutover applied new
  config before its compatible server image and crash-looped until image automation caught up;
  independent server/static policies can repeat that skew. A failed promotion must leave the last
  complete tuple serving.
- **Prove retry-safe tool-call admission.** Add fault injection for "the durable admission commit
  succeeded, the HTTP/MCP response was lost, and the caller retried." Specify and implement a
  caller-visible idempotency key scoped by canonical Operator and Agent binding if the current path
  can create two executions. Preserve the exact binding generation in the deduplication boundary.
- **Add public-client abuse controls if public DCR remains enabled.** Bound enrollment and
  registration attempts with Haku-side rate limits and transaction quotas. FastMCP continues to
  own protocol validation and TTLs; Haku owns enrollment interaction and activation limits.

## Agent and Operator product work

The canonical authority/enrollment cutover and migration squash are complete. These are product
slices over the deployed schema, not another identity migration:

- **Connected Agents:** ship an Operator-scoped API and trusted-console UI showing canonical Agent
  name/status, safe client metadata, scopes, binding generations, creation time, last-authenticated
  time, and reconnect history. Do not call inactivity "disconnected" or expose secrets/raw OAuth
  metadata. The existing schema is sufficient for this slice; do not block it on schema cleanup.
- **Agent-filtered tool-call history:** add an optional canonical `agent_id` backend filter and UI
  control. Apply authenticated `operator_id` first and Agent scope second. A foreign Agent UUID
  returns an empty result rather than revealing existence.
- **Lifecycle controls:** stage Operator-owned revoke/disable, rename, and tombstone/reconnect
  history as separate API + UI + audit slices. Distinguish revoking one binding/grant from disabling
  the Agent and all usable bindings. Keep names required, normalized, and globally unique; decide
  whether rename history is a product requirement before adding another reservation/audit table.
- **Per-Agent approval policy:** store a typed policy keyed by canonical Agent, with the current
  global policy as inherited/default. Later derive each Agent's tool surface from the verified
  binding plus policy and emit `tools/list_changed` on policy edits; never authorize by an
  unverified OAuth `client_id`.
- **Per-tool-call deep link:** make the promise URL open/highlight its exact call rather than merely
  loading the console. Detailed route/UI pointer: <console/TODO.md>.

## Console and authority consolidation

Keep these as small mechanical PRs unless a wire-contract check finds an external consumer:

- Return pending `ToolCallRecord` values directly and remove the subset `PendingApproval` DTO plus
  frontend unions.
- Remove the impossible tool-level degraded-metadata variant; server-level degradation is the real
  boundary.
- Collapse identical access/refresh grant-resolution methods, ceremonial context wrappers, and
  dead exception types. Preserve the required FastMCP context bridge, exact actor/binding
  provenance, execution-time revalidation, and failure-preserving `MultiAuth` behavior.
- Collapse `/api/approvals/events`, its cursor table, and multiple per-transition broadcasts into
  one typed, Operator-routed `tool_calls_changed(tool_call_id)` invalidation. REST and actor-scoped
  database reads remain authoritative; PostgreSQL notification delivery remains a lossy wakeup.
- After the Connected Agents read contract provides evidence about which joins really hurt,
  simplify the authority schema: remove the deferred name-reservation ownership cycle and
  speculative client-software fields, consider the relational `operator_id`-or-`binding_id`
  discriminated union directly on tool calls, remove deployment-only `secret_reference`, and prune
  redundant trigger functions while retaining genuine cross-row security invariants. This is a
  deliberate migration, not a five-second cleanup.

## Google connection ownership and Airlock

The current Airlock-backed `gmail` and `google_calendar` tool surfaces are intentionally global to
authenticated Haku Agents during this transitional state. Do not add a temporary per-Operator
owner mapping around the singleton token. Introduce Operator scoping when Haku owns the downstream
Google connection itself:

- **Give Haku per-Operator Google connections.** Replace the singleton Airlock-issued
  `haku_console_google` token with Haku-owned connect/status/reconnect/revoke, private refresh-token
  storage, and execution-time Operator selection. This is a downstream-provider relationship,
  separate from Agent enrollment and Agent credentials.
- **Retire only Haku's Airlock dependency after live proof.** Remove the
  `haku_console_google` provider, its access-token publication/External Secrets mirror, and the
  haku-console token mount. Do not couple this to Airlock's unrelated Oura, BSC, OpenClaw, or other
  remaining consumers, and do not treat broader Airlock retirement as a prerequisite.
- **Decide `haku_routine` ownership independently.** The Google singleton decision does not define
  whether every Operator should share one routine launcher. Specify whether the launcher is a
  global Haku capability or an Operator-owned downstream resource before relying on it in a
  multi-Operator console.

## Cross-cutting OAuth/Auth infrastructure

- **Minimum Airlock hardening while it remains:** separate interactive proxy, browser Operator,
  Claude Code, and OpenClaw issuer/audience/scope contracts; require authenticated management POST
  to initiate provider connection; make callback state bounded, expiring, one-time,
  initiator/action/provider/generation-bound, and consumed on every terminal path.
- **Typed auth configuration:** replace optional-heavy incoming/outgoing auth configurations with
  role-specific discriminated models and typed scope domains, atomically per consumer. Keep
  credentialed-facade and identity-delegation constructors separate.
- **Shared browser OIDC helper:** extract only genuinely common Authlib/Starlette relying-party
  behavior from Haku and Props. Migrate Study Casino away from username authority to a local UUID
  plus exact `(issuer, subject)` identity.
- **Singular Authentik ownership:** inventory provider/application/controller ownership, resolve
  duplicate ownership such as Kagent proxy-vs-OIDC, assign shared mappings one controller, update
  `cluster/docs/mcp_oauth_authentik_notes.md`, and add drift checks.

## Repo-boundary follow-ups (from the 2026-07-07 state_template retirement)

- **Source access recipes — decide the long-term home:** today `base/sources/` keeps
  per-channel contracts + generic recipes (multi-agent-reusable ones, e.g. ActivityWatch,
  point at `cluster/docs/`), while Haku's living helpers/runbooks are in haku-state. If
  base recipes keep going stale against state-side reality, slim the rest of
  `base/sources/` to thin contracts the same way ActivityWatch was.

## New read-only sources to wire

Each follows the same pattern: a read-only credential or filter facade reachable
from `haku-sandbox`, plus a source guide in `base/sources/` (and any reusable
technique as a pass in haku-state's `procedures/`).

- **Tana workspace** — read-only Tana access. **Built + wired:** the `tana-mcp-ro`
  facade (in `tana-mcp`) fronts the Tana MCP, exposes only read tools (default-deny
  allowlist via `MCP_FACADE_TOOLS__ALLOW`), injects the Tana PAT server-side
  (callers never see it), and is gated by the static bearer `haku-tana-ro-token`
  (reflected into `haku-sandbox`). It's published at the bearer-gated route
  `tana-mcp-ro.allegedly.works`, Haku's closure carries the `fastmcp` client, and
  the `tana` playbook + the `haku-tana-ro-token` credentials row use it.
  Remaining:
  - Confirm the read-only allowlist against the live `tools/list`; settle the
    `get_or_create_calendar_node` exclusion (it can create a daily node).
  - **Consider stronger auth for the public route (if/when feasible):** it's gated
    only by the long-lived static bearer today. The same facade image already
    supports Authentik OIDC — the read-write `tana-mcp-facade` runs that way
    (`MCP_FACADE_AUTH__OIDC_*`, group-enforced, Valkey OAuth state) — which buys
    short-lived tokens, central revocation, and audit, with Haku using
    `fastmcp --auth oauth` so no read token ever touches a command line. Read-only
    tools + the server-side PAT keep the blast radius small, so this is hardening,
    not a blocker.
  - **Future (PLAN north star):** give Haku `mcp__tana_ro__*` tools natively via a
    `.mcp.json` `http` entry to the route (bearer threaded from the reflected
    secret), so `tana` can drop its connection section and the explicit
    `fastmcp` step entirely.
- **Cluster Forgejo repos** — read access to `ducktape` and `gaffer-private`
  if/when they're migrated or mirrored to the cluster Forgejo: grant the `haku`
  Forgejo user read, add a repo-activity playbook (open PRs/issues/review
  requests needing attention). `gaffer-private` stays private.
- **ActivityWatch** — read-only access to activity-tracking data once it's ready
  (currently suspended; see `cluster/` ActivityWatch). Useful for time-use
  patterns and "what changed in your routine" reasoning (e.g. cross-referencing
  CPAP weekend leakage with weekend activity).
- **Google scopes** — the airlock grant now carries Gmail, Calendar, Drive
  (+ activity), Contacts, Docs, Sheets, Slides, Tasks, and YouTube read-only.
  Re-consent at airlock's OAuth Providers page after adding scopes for the live
  token to pick them up (the Drift row flags what's missing). Google **Keep** is
  not pursuable on this account — its API is Workspace-only and this is a personal
  Google account — so `keep_notes` stays an illustrative example only. Further
  read-only Google scopes light up the same way as added.

## Read-only filter facades (sources designed, not yet wired)

These MCP servers expose mutating tools, so each needs a **read-only filter
facade** in front (the Authentik OAuth facade is auth, not tool filtering — see
`PLAN.md` → _Access model_) before Haku may use it:

- **PostScanMail** — unopened-mail → open/discard/shred suggestions. First filter
  facade to build; also proves the `client_credentials` facade-auth path.

**Grocy is wired** (`base/sources/grocy.md`) — it didn't need a tool-filter facade:
the `haku` Grocy user has empty permissions, so the Grocy API enforces read-only
(200 reads / 403 writes) server-side. Haku reaches the grocy-sf MCP directly with a
rotated JWT, mirrored into `haku-sandbox` by ESO as `haku-cloud-grocy-sf-token`.

## Autonomous write capabilities

Haku's current contract has free tools plus approval-gated tool-call requests. This section is for
new **free/autonomous** write tools: capabilities Haku may exercise without per-call operator
approval because the server-side boundary makes them safe by construction. Wiring one on is still a
doctrine change, not just a config line.

## Wiring / hardening

- **Verify the JWT mint** — confirm the `authentik-jwt-rotation` CronJob produces
  `secrets/haku-k8s-jwt.yaml` (the `client_credentials`-as-`haku-k8s` flow with
  `expected_group: haku`). The web home's whole kubectl path depends on it.
- **Haku LiteLLM key** — `tf/gitops/litellm-api-key` → a `haku-sandbox` secret
  for attribution / budget / kill-switch, if routing model calls through LiteLLM.
- **Tighten egress** — narrow the `haku-sandbox` CCNP `toEntities: cluster` to
  only Haku's named in-cluster sources (the gap `claude-sandbox` also accepts).
- **SA→group scope-mapping allowlist** — replace the
  `kubectl_sandbox_fixed_groups` `else` default with an explicit SA→group map
  once the claude-sandbox JWT path has soaked (`tf/gitops/agent-machine-access`).

## Console — operator-facing dashboard

The console design + action model live in `console/README.md`; the free-form-UI
contract in `console/docs/containment.md`. (The launch-routine button itself
has shipped on the capability tier — see the README.)

- **Finish moving launch off the capability tier onto MCP approvals.** The `haku_routine`
  in-process MCP server (`console/tools/routine.py`, tool `launch_routine`) now fires the
  routine through the standard approval queue; the bespoke `capabilities.py` launch path and
  the `requestLaunch` bridge verb are kept only for the transition. Remaining:
  1. **haku-state:** migrate haku-ui to submit a `launch_routine` tool call through its backend
     (the path it already uses for other tool calls) instead of posting `requestLaunch` over the
     bridge; drop its `requestLaunch` usage + its own launch dialog (the approval drawer renders
     the prompt now).
  2. **ducktape:** once haku-ui is migrated, delete the launch-routine capability endpoint +
     `LaunchRoutineRequest`, the `requestLaunch`/`launchResult` bridge protocol verbs
     (`haku/shared/bridge_protocol/`), and the shell's launch `ConfirmDialog` branch; relocate the
     shared `GET /api/capabilities/csrf` endpoint (used by the approval + operator-auth flows) so
     `capabilities.py` can be removed.
- **Recent routine executions + one-in-flight guard.** A read-only panel listing recent
  runs of the claude-code-web routine (status, start time, link), and a guard that blocks
  a second launch while one is in flight so a stray click can't fan out sessions. Both
  need a routine-runs **listing** API — **none is known to exist** for `claude_code`
  routines (only `/fire`), so until one surfaces the interim affordance is the deep-link
  to the routine's `claude.ai/code` page (already surfaced in the console). When a listing
  API exists, build the panel and adopt the `anthropic` Python SDK for the Anthropic calls
  (migrating the launch POST onto it).
- **Canned per-fire routine instructions.** The launch dialog now supports ad-hoc per-run
  `text`; consider adding quick buttons for common instructions (e.g. "scan Gmail now",
  "CPAP check", "triage open PRs"). Reuses the launch button's existing bearer + egress
  perimeter. Docs: code.claude.com/docs/en/routines.

## Managed Agents runtimes — per-runtime TODOs

Runtime-specific TODOs live with each runtime (the agent loop runs at Anthropic;
the runtimes differ in where the sandbox runs — see
<runtime/managed_agent/README.md>):

- **Self-hosted worker (Runtime B)** — operator activation to go live:
  <runtime/managed_agent/self_hosted/TODO.md>.
- **Anthropic-hosted cloud** — **PARKED (2026-07-04)**: the cloud control-plane
  objects were deleted at Anthropic and `cluster/k8s/haku/cloud-agent-tf` is
  suspended; see <runtime/managed_agent/anthropic_hosted/PLAN.md> for the reason
  and the resume decision. Per-runtime TODO (mostly moot until resumed):
  <runtime/managed_agent/anthropic_hosted/TODO.md>.

## Later (post-v0)

- **In-cluster runtime** — realized as `runtime/agent` (Runtime C, MAF
  self-hosted loop) and `runtime/managed_agent/self_hosted` (Runtime B, Managed
  Agents self-hosted worker; remaining wiring in its per-runtime TODO above). The
  old `haku-scanner` image + CronJob idea is superseded.
- **haku-traces** — push Claude Code transcripts to a store separate from
  `haku-state` for replayability.
- **tier-2 execution** — haku-owned execution behind stronger gating, only if
  handoff-via-prompt proves too slow for routine actions.
- **Precise effort/cost model** — today effort budgeting is a rough heuristic
  (operator value-of-time anchor in `memory/` vs. a hand-wavy "tokens loosely track
  cost" proxy; see `instructions.md` → effort budgeting). Make it concrete: actual
  per-run token/$ accounting (e.g. from LiteLLM/Langfuse), a real estimate of model
  cost (e.g. Opus 4.8 per-token), and a defensible mapping from "agent effort" to
  "value of the operator's time" so Haku can decide research depth on more than a vibe.
