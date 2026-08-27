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
- **Surface console/runner skew, and give the operator a way to cycle runners.** A session's runner
  pod is created when the session is provisioned and pinned to whatever `haku-harness-runner` tag was
  current at that moment, so a long-lived session drifts arbitrarily far behind the console with
  nothing anywhere reporting it. Measured 2026-08-16: the single live session's runner had been up
  since 2026-08-15T07:12 on `devel-20260815044840-88846f1` while the `SandboxTemplate` was already
  on `devel-20260816120126-31f5ae3` — and the visible consequence was that **0 of 35,760 production
  frames carried `runner_seq`**, because #4166's frame numbering shipped in an image no running
  runner had. Neither the console, the room, nor the frame log records a runner build at all, so the
  skew is only findable by reading pod specs. Wanted: the runner reports its build in the protocol
  handshake and the console stores it on the session; a runner older than the console's expected
  floor is visibly marked rather than silently degraded; and restarting a runner is an operator
  action on the session instead of `kubectl delete pod` in a namespace the console's own agent
  identity cannot reach. The restart stays the operator's call, because it ends the live turn.
  **Deleting the pod is not the restart.** Tried on 2026-08-16: the `Sandbox` owning it recreated
  it within seconds on the same `devel-20260815044840-88846f1`, because the pod template was
  rendered from the `SandboxTemplate` when the claim was made and the tag is pinned there, not
  resolved per pod. So cycling a session onto a newer runner means replacing the `Sandbox` — which
  ends the session rather than the turn, and is why this wants a real action with a warning rather
  than an operator reaching for `kubectl`.
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
- **`tools/list_changed` when an Agent's policy changes:** the policy graph is already per-Agent and
  config-driven (`console/auto_approval.py`'s registry over the `auto_approval_policies` and
  per-agent `auto_approval_policy:` keys in `cluster/k8s/haku/console/config.yaml`), and an Operator
  can reassign an OAuth Agent's policy in Settings. What is missing is the notification: a connected
  client enumerated its tool surface once, so an edit that moves a tool between pass-through and
  approval-wrapped does not reach it until it reconnects.

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
  directly for Drive/Tasks and as the Gmail/Calendar REST fallback. Target: the
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

## New read-only sources to wire

Each follows the same pattern: a read-only credential or filter facade reachable
from `haku-sandbox`, plus a source guide in haku-state (and any reusable technique
as a pass in its `procedures/`).

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

**Grocy is wired** — routed through haku-console's `grocy-sf`
MCP entry (the `grocy_reads` policy in `cluster/k8s/haku/console/config.yaml` auto-approves the
read tools; every write tool stays approval-gated). Haku's dedicated read-only `haku` Grocy identity
(`grocy-mcp-haku-sf` Authentik provider, its JWT rotation, and the ESO reflection into
`haku-sandbox`) was retired — console-side allowlisting needed no separate credential,
and unlike the direct read-only path it also lets every runtime reach approval-gated
Grocy writes.

**Tana is wired** — routed through haku-console's `tana-rw`
MCP entry instead of a dedicated facade: the read tools (`search_nodes`, `read_node`,
`get_children`, `open_node`, `list_tags`, `list_workspaces`, `get_tag_schema`) plus the
idempotent `get_or_create_calendar_node` auto-approve under the `tana_safe_tools` policy in
`cluster/k8s/haku/console/config.yaml`; every write tool stays approval-gated. The
standalone `tana-mcp-ro` facade (`cluster/k8s/agents/tana-mcp-ro/`) was retired —
console-side allowlisting needed no separate Deployment/secret/route.

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
  <runtime/managed_agent/self_hosted/README.md> and its bring-up RCA.
- **Anthropic-hosted cloud** — **PARKED (2026-07-04)**: the cloud control-plane
  objects were deleted at Anthropic and `cluster/k8s/haku/cloud-agent-tf` is
  suspended; see <runtime/managed_agent/anthropic_hosted/README.md> for the reason
  and the resume decision. Per-runtime TODO (mostly moot until resumed):
  <runtime/managed_agent/anthropic_hosted/TODO.md>.

## Later (post-v0)

- **In-cluster runtime** — realized as `runtime/agent` (Runtime C, MAF
  self-hosted loop) and `runtime/managed_agent/self_hosted` (Runtime B, Managed
  Agents self-hosted worker; remaining wiring in its per-runtime TODO above). The
  old `haku-scanner` image + CronJob idea is superseded.
- **haku-traces** — push Claude Code transcripts to a store separate from
  `haku-state` for replayability. Prefer `OTEL_LOG_RAW_API_BODIES=file:<dir>` over
  parsing transcript JSONL: untruncated bodies as JSON plus a `body_ref` join key
  back to the Loki events — see
  [transcript collection](../devinfra/claude/plans/transcript_collection.md) § _Raw API bodies_.
  If the loop ever runs on the **Claude Agent SDK**, a second mechanism exists: a
  `SessionStore` adapter (`session_store` on `ClaudeAgentOptions`; `append`/`load` required,
  `list_sessions`/`list_session_summaries`/`delete`/`list_subkeys` optional) — docs:
  <https://code.claude.com/docs/en/agent-sdk/session-storage>. **Deliberately not done
  first:** the adapter runs _inside_ the sandbox, so pointing it at the console's Postgres
  would give a deliberately fenced pod egress to — and credentials for — a database outside
  its perimeter. That trades the force-proxy fence for convenience. The direction to explore
  instead is inverting it: the sandbox keeps writing local JSONL
  (`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-id>.jsonl`) and the console _pulls_,
  or the transcript ships out over the MCP path that is already permitted. Worth knowing
  before building either way: the store is a best-effort **mirror** of the local file, not a
  replacement (a dropped batch surfaces only as a `{type:"system", subtype:"mirror_error"}`
  message, so it needs handling if "all transcripts" is the goal); retries can re-deliver, so
  dedupe on `entry.uuid`; `load()` returns the full raw history including pre-compaction turns
  that `get_session_messages_from_store()` has already collapsed into a summary; and it refuses
  to combine with `persist_session=False` or file checkpointing. A conformance suite ships in
  the package (`claude_agent_sdk.testing.run_session_store_conformance`), so an adapter is
  testable without any Anthropic credentials.
- **A memory-flush trigger.** Nothing currently nudges Haku to write durable notes
  into `haku-state` before compaction — it runs on model goodwill, unlike OpenClaw's
  pre-compaction flush (a silent turn reminding the agent to save to memory files).
  Claude Code's `PreCompact` hook cannot reproduce it: it can block compaction or
  return `additionalContext`, but that lands _after_ compaction and it cannot make
  the agent take a turn. Two primitives that can: a **`Stop` hook** (can block the
  stop and inject `additionalContext`, so needs gating — e.g. only when `haku-state`
  has no commit this session), or a **`type: "agent"` hook on `PreCompact`** that
  extracts to `haku-state` out-of-band without spending the main session's context.
  Note `type: "command"` hooks are unsupported in Claude Code web, so this must be
  `http` / `agent` / `prompt` / `mcp_tool` — an `http` or `mcp_tool` hook against
  haku-console keeps the logic in reviewed code. Works on Runtime A today; not
  coupled to the runtime question. Under the Agent SDK this is simpler still —
  hooks there are in-process callbacks and Python has both `PreCompact` and `Stop`
  (see <plans/agent_sdk_sandbox_runtime.md>).
- **Cut the sandbox over to the Nix image** — `cluster/k8s/haku/workspaces/image/default.nix`
  builds in CI and publishes to `haku-sandbox-image-nix`, but the SandboxTemplate still pulls
  the apt/Dockerfile build. The blocker is a **runtime** question a green build can't answer:
  whether the Bazel bazelisk downloads (and `rules_python`'s hermetic CPython) can find
  `libstdc++.so.6` under NixOS glibc. Run the checklist in
  <../cluster/k8s/haku/workspaces/image/README.md> § _Cutting over_ against a throwaway Pod;
  if it passes, delete the Dockerfile and collapse the two workflows into one. If it fails,
  try `nix-ld` via pod env before abandoning it. **Depends on nothing** — the probe needs no
  change to the template, the warm pool, or the MCP config.
- **Deduplicate the agent pod images** — once the Nix cutover lands, the Haku sandbox image
  and <../x/codex_pod_image/default.nix> share a real substrate (git, tea, jq, curl, kubectl,
  cacert, tini, the coreutils shell set) that is currently written out twice. Extract the
  common `buildEnv` paths into one module and let each image add only its distinctive tools
  (bazelisk/JDK/cc for Haku; codex/claude-code/ssh for the codex pod). Not worth doing before
  the cutover — the shared list is speculative until the Nix image is the real one.
- **Auto-sync the Forgejo ducktape mirror, and decide whether agents may PR against it** —
  `forgejo-http.forgejo:3000/haku/ducktape.git` exists but is not automatically mirrored, and
  measured 3 commits behind `devel` (`97a23895` vs `a4c497f7`) on 2026-07-25. The sandbox
  bootstrap therefore clones ducktape from **GitHub** instead, which works fine and is always
  current, so nothing is blocked on this — but a stale in-cluster mirror is a trap for anyone
  who reaches for it (base-sync against it silently under-reports contract changes). Either
  mirror it on a schedule/webhook and point the bootstrap at it, or delete it so it can't be
  picked up by mistake. The open design question is whether agents should be able to open PRs
  against the in-cluster copy at all, and how those would flow back to GitHub.
- **Harmonize the Forgejo host** — the same Forgejo is reached under two names and
  every consumer has to know which: `forgejo-http.forgejo` in-cluster (git clones,
  the `ducktape_haku` bzlmod `git_override`) and `git.allegedly.works` publicly
  (the CLI's REST readers, `tools/ci_wait.sh`). Credentials, `.netrc` entries, and
  `NO_PROXY` all have to be written twice, and a missing second entry fails as a
  bare 404 (`haku read --source cpap` in the sandbox, 2026-07-24). Pick one name
  that resolves both inside and outside the cluster and collapse the duplication.
- **tier-2 execution** — haku-owned execution behind stronger gating, only if
  handoff-via-prompt proves too slow for routine actions.
- **Precise effort/cost model** — today effort budgeting is a rough heuristic
  (operator value-of-time anchor in `memory/` vs. a hand-wavy "tokens loosely track
  cost" proxy; see haku-state's effort-budgeting guidance). Make it concrete: actual
  per-run token/$ accounting (e.g. from LiteLLM/Langfuse), a real estimate of model
  cost (e.g. Opus 4.8 per-token), and a defensible mapping from "agent effort" to
  "value of the operator's time" so Haku can decide research depth on more than a vibe.
- **Narrow the sandbox's GitHub grant, and make it steerable at runtime** — the Claude
  sandbox now reaches GitHub as `agentydragon-agent`, and that grant is all-or-nothing: the
  egress proxy substitutes the PAT for any request to `github.com` / `api.github.com` /
  `codeload.github.com`, so every repo the account can touch is in scope for the whole
  session. Two wants, roughly independent. **Per-repo scoping:** the proxy already sees the
  request path, so a rule could allow `agentydragon/ducktape` and refuse the rest — the
  cheaper half, and it turns a standing grant into a reviewable list. A GitHub App
  installation token scoped to selected repositories would enforce it at the far end instead,
  which is stronger and more work. **Runtime control:** being able to widen or revoke what
  the proxy permits mid-session, rather than only by editing a manifest and rolling the pod —
  the same shape as the approval queue, applied to egress rather than to tool calls.
