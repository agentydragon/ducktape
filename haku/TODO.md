# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done. Component-specific detail stays in the
component checklist rather than being copied here: haku-console's tool/API backlog is
<console/TODO.md>, while the remaining OAuth/identity rationale and sequencing is
<../plans/oauth_architecture.md>.

## Immediate correctness and security

- **Make independent haku-console rollouts skew-safe.** Do not bundle the server image, static
  image, and live config into a bespoke atomic promotion unit. Define and test compatibility across
  one rollout window instead: deploy readers before writers for config changes; make server API
  additions available before the static frontend consumes them; remove old fields/endpoints only
  after every consumer has moved. CI should exercise the server against the current and next config
  shapes and the frontend against its supported server contract. Revisit the current `Recreate`
  strategy so a failed replacement does not discard the last serving version. The 2026-07-14
  authority cutover is the regression case: new config restarted the old incompatible server and
  caused an outage until image automation caught up.
- **Prove retry-safe tool-call admission.** Add fault injection for "the durable admission commit
  succeeded, the HTTP/MCP response was lost, and the caller retried." Specify and implement a
  caller-visible idempotency key scoped by canonical Operator and Agent binding if the current path
  can create two executions. Preserve the exact binding generation in the deduplication boundary.
- **Recover tool calls stranded in `RUNNING` without guessing their external outcome.** Add fault
  injection for a pod/process loss both after the `RUNNING` commit but before backend execution and
  after backend success but before terminal persistence. Specify an explicit unknown-outcome state
  plus attempt/lease ownership, surface stale calls to the Operator, and reconcile them without
  blindly retrying non-idempotent tools. A timeout alone must not turn an unknown external outcome
  into either success, failure, or a second execution.
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

- After the Connected Agents read contract provides evidence about which joins really hurt,
  simplify the authority schema: remove the deferred name-reservation ownership cycle and
  speculative client-software fields, consider the relational `operator_id`-or-`binding_id`
  discriminated union directly on tool calls, remove deployment-only `secret_reference`, and prune
  redundant trigger functions while retaining genuine cross-row security invariants. This is a
  deliberate migration, not a five-second cleanup.

## Google connection ownership and Airlock

- **G3 (later, not scheduled): retire Haku's last Airlock dependency — the read-only Google token.**
  The console now owns Gmail/Calendar (G1/G2, done), but the agent still holds the read-only
  `google-access-token` (`$TOK`) that the `google` Airlock grant reflects into `haku-sandbox` — used
  directly for Drive/Tasks and as the Gmail/Calendar REST fallback (`base/sources/`). Target: the
  console mediates all Google access so the agent holds no standing Google token; high-risk ops are
  already approval-gated (invariant), and the open question is whether to also move read-only reads
  behind console MCP tools (cleaner/safer, but a larger tool surface) vs. keep the direct read-only
  token. Full G-sequence, target, and tradeoff: `plans/google_access_mediation.md`.
- **Decide `haku_routine` ownership independently.** The Google singleton decision does not define
  whether every Operator should share one routine launcher. Specify whether the launcher is a
  global Haku capability or an Operator-owned downstream resource before relying on it in a
  multi-Operator console.

## Cross-cutting OAuth/Auth infrastructure

- **Keep Airlock credential-only while it remains:** it may own provider consent, refresh-token
  custody, and access-token publication. Do not add MCP ingress, tool execution, agent policy, or
  an operator-approval queue back to it; those are Haku Console responsibilities.
- **Typed auth configuration:** replace optional-heavy incoming/outgoing auth configurations with
  role-specific discriminated models and typed scope domains, atomically per consumer. Keep
  credentialed-facade and identity-delegation constructors separate.
- **Shared browser OIDC helper:** extract only genuinely common Authlib/Starlette relying-party
  behavior from Haku and Props. Migrate Study Casino away from username authority to a local UUID
  plus exact `(issuer, subject)` identity.
- **Singular Authentik ownership:** inventory remaining provider/application/controller ownership,
  assign shared mappings one controller, update `cluster/docs/mcp_oauth_authentik_notes.md`, and add
  drift checks.

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

## Mutating-tool sources behind haku-console

A source whose MCP server exposes mutating tools doesn't need a separate read-only filter
facade: wire the full server behind haku-console (`cluster/k8s/haku/console/config.yaml`) and
let the console's approval gate filter it — reads auto-approve, every mutating/paid/destructive
call queues for operator approval (`haku/console/auto_approval.py`). The Authentik OAuth facades
are auth, not tool filtering, but the approval gate is. PostScanMail is wired this way.

**Grocy is wired** (`base/sources/grocy.md`) — routed through haku-console's `grocy-sf`
MCP entry (`GROCY_READ_TOOLS` in `console/auto_approval.py` auto-approve; every write
tool stays approval-gated). Haku's dedicated read-only `haku` Grocy identity
(`grocy-mcp-haku-sf` Authentik provider, its JWT rotation, and the ESO reflection into
`haku-sandbox`) was retired — console-side allowlisting needed no separate credential,
and unlike the direct read-only path it also lets every runtime reach approval-gated
Grocy writes.

**Tana is wired** (`base/sources/tana.md`) — routed through haku-console's `tana-rw`
MCP entry instead of a dedicated facade: the read tools (`search_nodes`, `read_node`,
`get_children`, `open_node`, `list_tags`, `list_workspaces`, `get_tag_schema`) plus the
idempotent `get_or_create_calendar_node` auto-approve under the console's reviewed
policy (`console/auto_approval.py`); every write tool stays approval-gated. The
standalone `tana-mcp-ro` facade (`cluster/k8s/agents/tana-mcp-ro/`) was retired —
console-side allowlisting needed no separate Deployment/secret/route.

## Autonomous write capabilities

Haku's current contract has free tools plus approval-gated tool-call requests. This section is for
new **free/autonomous** write tools: capabilities Haku may exercise without per-call operator
approval because the server-side boundary makes them safe by construction. Wiring one on is still a
doctrine change, not just a config line.

### haku-state follow-up: seed the `haku-bash` sandbox pool

The ducktape side of `haku_sandbox/{reserve,exec,info}` is intentionally complete without putting
Haku-authored workloads in this repository. Before enabling live calls, make this follow-up PR in
`haku-state` (Flux already has the narrowly-scoped template/pool RBAC in ducktape):

- [ ] Add `k8s/sandbox/sandboxtemplate-haku-bash.yaml` with
  `apiVersion: extensions.agents.x-k8s.io/v1beta1`, `kind: SandboxTemplate`, and
  `metadata.name: haku-bash`. Set `spec.networkPolicyManagement: Unmanaged` so the existing
  `haku-sandbox` egress perimeter remains authoritative. Its pod template must have one container
  named `sandbox`, use a pinned Debian/bash image that includes GNU `/usr/bin/timeout`, run
  `sleep infinity`, set `workingDir: /workspace`, disable ServiceAccount-token automounting,
  drop all capabilities, and provide bounded requests/limits. Mount writable `/workspace` (a small
  SeaweedFS RWO volume via `volumeClaimTemplates`, preferred so state survives a pod recreation)
  and `/tmp`; the rest may stay read-only.
- [ ] Add `k8s/sandbox/sandboxwarmpool-haku-bash.yaml` with
  `apiVersion: extensions.agents.x-k8s.io/v1beta1`, `kind: SandboxWarmPool`,
  `metadata.name: haku-bash`, `spec.replicas: 1`, `updateStrategy.type: Recreate`, and
  `sandboxTemplateRef.name: haku-bash`. This is a new Haku-only pool; do not point the tools at the
  operator's `agent-workspaces` pools.
- [ ] Add both resources to haku-state's `k8s/kustomization.yaml`. Do **not** commit runtime
  `SandboxClaim`s there: Flux inventory/prune must own only the template and warm-pool capacity;
  haku-console owns live claims.
- [ ] After both repos reconcile, smoke-test `haku_sandbox/reserve`, then
  `haku_sandbox/exec` with `{"cmd":["bash","-lc","echo ok"],"timeout_ms":10000}`, and verify
  `info` reports `ready` plus an eight-hour deadline. Patch a test claim's deadline into the past
  and verify it becomes `expired` (not immediately `not_found`) while its Pod/Sandbox disappear.
- [ ] V2 only: add an optional claim-time startup hook (for example, refresh a checkout) and do not
  report the handle ready until it succeeds. V1 deliberately skips this optimization; callers can
  run setup as their first `exec`, and every exec already renews the lease before and after work.

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
