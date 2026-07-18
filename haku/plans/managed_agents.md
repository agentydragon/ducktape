# Plan: Haku on Anthropic Managed Agents (self-hosted sandbox)

Status: **design / exploring — Haku's runtime is not yet decided.** The current
operational path is Runtime A (Claude Code web routine), kept despite its lock-in
and the options it disallows; this is the detailed design for candidate B, kept
open for evaluation alongside C. Expands the _Alternative runtime: Anthropic
Managed Agents_ section of <../PLAN.md> from a paragraph into a concrete migration
design, and is an alternative to the deferred in-cluster `haku-scanner` runtime
(<../TODO.md> → _Later_). An experimental self-hosted worker landed at
`haku/runtime/managed_agent/self_hosted/` (see
[managed_agents_artifacts.md](managed_agents_artifacts.md)), but adopting B as
Haku's runtime is not yet committed to.

## Why

v0 runs Haku as a **manually-started, ephemeral Claude Code web session** on
Anthropic infra (`haku/runtime/claude_web_env/`). Two wants motivate moving off it:

1. **Trigger Haku on events / on demand** (a push to `haku-state`, a schedule, a
   button) — without a human opening a web session.
2. **Don't re-read everything on every wake.** A fresh session re-bootstraps the
   environment and re-loads the operating manual as cold context each time.

Anthropic **Managed Agents** (public beta, `managed-agents-2026-04-01`) is
phase-1-as-a-service: Anthropic runs the agent loop; you supply a persisted,
versioned **agent** config and an **environment**, and drive **sessions** over an
event stream. Its **self-hosted sandbox** (`config.type: "self_hosted"`) is the
bring-your-own-container variant: the loop stays at Anthropic, but tool execution
(`bash`, file ops, code) runs in a **worker you run in `haku-sandbox`**, polling
Anthropic's work queue outbound-only. That is exactly Haku's posture today (a
read-only container behind the mitmproxy egress), so the fit is unusually close.

This **removes two specced-but-unbuilt work items**: the MCP
`client_credentials` / facade-JWT spike (replaced by **vaults**, below), and the
`haku-scanner` image / CronJob upkeep (replaced by the worker image plus a
supervisor). It **gives up** in-cluster LiteLLM/Langfuse attribution and the
`haku-traces` transcript store — the loop and its traces live at Anthropic (see
_Tradeoffs_).

## Runtime model: one long-lived session + wake events

The design we want (not a session-per-run):

- The **supervisor** keeps **exactly one live Haku session** and holds it open.
  On a trigger it sends a `user.message` event — _"do a scan per `run.md`"_ —
  into that session. Events queue and process in order, so a wake that arrives
  mid-run is picked up next. The agent **retains its loaded base manual and
  recent reasoning across wakes**, so a wake is an incremental nudge, not a cold
  re-read. This is the literal answer to want (2).
- **Idle is cheap.** Session-runtime billing accrues only while a session is
  `running`; an idle session waiting for the next wake does not (verify — see
  _Open questions_). Token cost is paid per wake (cached history), not per idle
  minute.
- **Context stays bounded.** Managed Agents sessions auto-**compact** history near
  the context limit, so a long-lived session doesn't blow the window.
- **`haku-state` is still the system of record.** The warm session is an
  optimization, not memory. If it terminates or compacts, a fresh session just
  re-orients from `haku-state` (its `memory/` bookmarks + `items/` + `log/`) — as
  today. Losing the session never loses memory. So the supervisor may recreate
  the session at will, and the agent's own incremental-scan discipline
  (bookmarks) already means "only look at what's new" independent of session
  warmth.

```
trigger (push / cron / button)
        │
        ▼
   supervisor ──holds──▶ one Haku session ◀──loop runs at──▶ Anthropic
   (holds API key)        │  events: user.message in,            orchestration
   - 1 live session       │  agent.* / tool_use out
   - reconnect SSE        ▼
   - recreate on death   worker pod in haku-sandbox  ── bash/kubectl/psql/curl/git
                          (holds ANTHROPIC_ENVIRONMENT_KEY only)   ▲
                          bootstrap.sh: kubeconfig + clone state ──┘
```

### Triggers funnel into the supervisor

All paths end in "send a wake message to the current session (creating one if
none is live)" — **not** `sessions.create()` per event:

- **Schedule** — the supervisor's own timer. (A Managed Agents _scheduled
  deployment_ fires a **new** session per tick, which is the cold, session-per-run
  shape we're explicitly avoiding — so the schedule lives in the supervisor, not a
  deployment.)
- **Git events** — a Forgejo webhook on `haku-state` (and/or ducktape base
  changes) → an HTTP endpoint on the supervisor → wake. Anthropic's own webhooks
  are Anthropic→you _notifications_, not GitHub→you _triggers_; the receiver is
  ordinary code on our side.
- **On demand** — a button / small CLI → the same wake endpoint.

## MCP servers + vaults (the "nicely handled auth" path)

Haku's sources today are reached ad hoc: Plaid over `psql` (in-cluster pod),
Gmail/Calendar/Tana over haku-console's aggregated MCP catalog (`fastmcp` to
`https://haku.allegedly.works/mcp`, since superseding their earlier dedicated
facades — <../TODO.md>). There is no `.mcp.json`. The PLAN north star (<../TODO.md>)
is to give Haku **native MCP tools**.

Managed Agents does this cleanly, and its **vaults** are precisely the
headless-MCP-auth mechanism the PLAN's _MCP auth provisioning_ spike was for:

- **Declare servers on the agent** — `mcp_servers: [{type:"url", name, url}]` plus
  a `tools: [{type:"mcp_toolset", mcp_server_name}]` entry. No auth on the agent.
- **Auth lives in a vault** — `mcp_oauth` (OAuth with **auto-refresh**) or
  `static_bearer`, keyed by the MCP server URL. Attach via `vault_ids` on the
  session. Anthropic injects the credential **at egress**, so the sandbox never
  sees it, and refreshes OAuth tokens itself. This is the "nicely handled auth for
  remote MCP servers" — and it deletes the `client_credentials` + JWT-rotation +
  "does the facade accept service-account JWTs" spike.

Candidate servers to wire (each already has or could expose a gated public route):
`haku-console` (already wired this way for Tana + Grocy: a `static_bearer` bound to
the console's aggregated `/mcp`, superseding a per-source `tana-mcp-ro`-style
facade), PostScanMail, the Google Workspace MCP, Manifold — the
`cluster/k8s/agents/*-mcp` fleet, optionally fronted by the `mcp_infra` compositor
as a single endpoint.

### Two caveats that shape the source split

1. **MCP runs on Anthropic's orchestration layer, not in the worker.** In a
   self-hosted sandbox only `bash`/file/code execution moves to your container;
   `mcp_toolset` calls originate from Anthropic's side. So a vault-backed MCP
   server must be **reachable from Anthropic at a public, gated URL** (as
   `haku.allegedly.works` — haku-console's aggregated MCP endpoint — already is).
   Sources that stay loopback-only
   in-cluster can't be vault-backed `mcp_toolset`s — the worker reaches them via
   `bash`/`fastmcp`/`psql` as today (no vault, manual auth). **Per source: public
   gated facade + vault (nice auth) vs. in-cluster + bash (worker-reached).**
2. **`environment_variable` vault credentials are _not_ supported in self-hosted**
   (egress is yours, so there's nowhere for Anthropic to substitute the secret).
   That's fine: the secrets Haku's **bash** tools need — the `haku` kubeconfig/JWT,
   the Plaid `plaid-mcp-db-readonly` DSN, the `google-access-token`, the
   `haku-state-git-write` creds — stay materialized **in-container from
   `haku-sandbox` k8s secrets at bootstrap**, exactly as `bootstrap.sh` does now.

Clean rule: **MCP auth → vaults; everything bash reaches → in-container k8s
secrets.**

## Component mapping

| Today (Claude Code web)                                                        | Self-hosted Managed Agent                                                                                                                                                      |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Session start hook + `web_setup.sh` provisions the env                         | A **worker image** running `EnvironmentWorker` (Python/Go SDK) or `ant beta:worker poll` in `haku-sandbox`                                                                     |
| `haku/runtime/claude_web_env/bootstrap.sh` (kubeconfig, `.netrc`, clone state) | **Same script** as the worker entrypoint — self-hosted does _not_ auto-mount repos, so our existing bootstrap _is_ the mount                                                   |
| `Execute haku/runtime/claude_web_env/run.md` prompt                            | A `user.message` wake event                                                                                                                                                    |
| `--dangerously-skip-permissions` (Pod = trust boundary)                        | `permission_policy: always_allow` (same reasoning)                                                                                                                             |
| Cluster access via `kubeapi.allegedly.works` proxy                             | **Direct in-cluster access** (worker runs in `haku-sandbox`)                                                                                                                   |
| Claude Code native tools (mostly bash)                                         | `agent_toolset_20260401` (bash/read/write/edit/glob/grep) + `mcp_toolset`s                                                                                                     |
| `haku-state` git repo as durable memory                                        | unchanged                                                                                                                                                                      |
| base read from the ducktape checkout, reconciled via `memory/base-sync`        | unchanged — worker clones ducktape; agent `system` is a thin pointer ("read the manual + run procedure, then run") so base stays single-sourced and reconciliation still works |

The worker pod keeps the Ember posture intact: non-root, behind the existing
dedicated `haku-egress-proxy` egress, scoped `haku-sandbox` RBAC, ResourceQuota —
none of it relying on agent restraint. Egress is ours in self-hosted, which the
`haku-egress-proxy` + CCNP already enforce.

## Artifacts to build

First-cut, copy-pasteable drafts of every item below are in
<managed_agents_artifacts.md>.

1. **`haku.agent.yaml`** (control plane, version-controlled, applied via `ant
beta:agents create|update`): `model: claude-opus-4-8`, thin pointer `system`,
   `tools: [agent_toolset_20260401, mcp_toolset…]`, `mcp_servers: […]`,
   `permission_policy: always_allow`.
2. **`haku.environment.yaml`**: `config: {type: self_hosted}`.
3. **Worker image** (Bazel `oci_image`, the standard
   <../../cluster/docs/container-images.md> path): Python 3.13, `git`, `kubectl`,
   postgres client, `curl`, `fastmcp`, `/bin/bash`, the `anthropic` SDK. Entrypoint
   reuses `bootstrap.sh`, then runs `EnvironmentWorker(...).run()`.
4. **Supervisor** (small service): holds the control-plane API key, maintains one
   live session, reconnects the SSE stream losslessly (history `events.list` +
   dedupe; break only on terminated / idle-with-terminal-stop_reason), recreates
   the session on death, and exposes the wake endpoint (HTTP for the webhook +
   internal timer for the schedule).
5. **k8s** in `cluster/k8s/haku/`: a `haku-managed-agent` Deployment and a
   `haku-supervisor` Deployment, an `ANTHROPIC_ENVIRONMENT_KEY` secret (worker
   only) and an API-key secret (supervisor only — keep the org-scoped API key
   **off** the worker host so agent tool calls can't read it), and a Forgejo
   webhook → supervisor route.
6. **Vault(s)**: one vault holding the MCP credentials (the haku-console bearer,
   then others as wired); referenced by `vault_ids` on session create.

## Effort

A working v1 is **a few focused days**; robustness + per-source MCP exposure is
the longer tail. Reused vs. new:

- **Reused, near-zero change**: `bootstrap.sh`, the `haku-egress-proxy` egress, the
  `haku-sandbox` RBAC/quota, `haku-state`, the base manual + run procedure, the
  incremental-scan discipline. Haku's actual logic doesn't change.
- **New**: worker image + entrypoint (~½–1d, repo already builds `oci_image`s),
  `haku.{agent,environment}.yaml` + env key (~½d), the supervisor with robust
  reconnect (~1–2d — the bulk), the Forgejo webhook route (~½d), and per-MCP
  vault + public gated facade wiring (incremental, ~½d per source).

## Tradeoffs vs. the self-hosted Claude Code path

- **Lose**: in-cluster LiteLLM attribution / budget / kill-switch and Langfuse
  traces (the loop runs at Anthropic). Kill-switch becomes "archive the agent /
  revoke the environment key"; budget becomes Anthropic workspace limits;
  per-run replay becomes the Console session trace (below) — optionally the
  supervisor can still mirror transcripts to `haku-traces`.
- **Gain**: no scheduled-CronJob / scanner-image bespoke runtime, no
  `client_credentials` MCP-auth spike (vaults), warm-session wakeups, and a
  first-class Console UI.

## Observability

The Anthropic **Console** (`platform.claude.com/workspaces/default/sessions`)
renders each session as a chronological, chat-style trace — messages, thinking,
every `tool_use` + `tool_result`, per-step token usage — live and after the
fact. It works for self-hosted too: tool I/O round-trips through Anthropic's
orchestration layer, so it appears in the trace even though execution is on our
worker. This is the managed-agents equivalent of the claude.ai/code session view
we watch Haku in today, and arguably better for an unattended agent. Aggregate
token/cost lives in Console → Logs and the Usage & Cost Admin API.

## Open questions / to verify

- **Idle billing**: confirm an idle long-lived session accrues no
  session-runtime cost (only tokens per wake). Drives whether warm-session is
  actually cheap.
- **Workdir persistence across wakes**: does a self-hosted worker's `/workspace`
  survive across turns of one long-lived session? Haku tolerates either way
  (state is in git), but it affects whether re-clone is needed per wake.
- **MCP vaults on self-hosted**: confirm `mcp_oauth`/`static_bearer` vault creds
  work with `config.type: self_hosted` (strongly implied — MCP runs Anthropic-side
  and vaults are offered as the self-hosted workaround for env-var creds — but
  verify on the haku-console `static_bearer` credential).
- **Which facades to expose publicly** (gated) for vault-backed `mcp_toolset` vs.
  keep loopback-only and reach via worker `bash`/`fastmcp`. Per source.
- **Attribution**: accept the loss of LiteLLM/Langfuse, or have the supervisor
  push session transcripts to `haku-traces`? Console trace + Admin usage API may
  suffice.
- **Beta surface**: Managed Agents + self-hosted are public beta; the
  `EnvironmentWorker` helper is Python/Go/TS only (use Python). Pin SDK versions.
