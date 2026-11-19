# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done. Component-specific detail stays in the
component checklist rather than being copied here: haku-console's tool/API backlog is
<console/TODO.md>, while the remaining OAuth/identity work (backlog, rationale and sequencing) is
<../plans/oauth_architecture.md>.

## Google connection ownership

- **Decide `haku_routine` ownership independently.** The Google singleton decision does not define
  whether every Operator should share one routine launcher. Specify whether the launcher is a
  global Haku capability or an Operator-owned downstream resource before relying on it in a
  multi-Operator console.

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

## Mutating-tool sources behind agentplane

A source whose MCP server exposes mutating tools doesn't need a separate read-only filter
facade: wire the full server as an agentplane ActionGroup (`cluster/cdk8s/agentplane/staging.py`)
and let action policy filter it — reads go in a policy set bound to `claude-ai`
(`actions_staging_policies.py`), and every mutating/paid/destructive call waits for operator
approval. Backend authentication (a static bearer, or the operator's OAuth linkage) is separate
from that tool policy.

## Wiring / hardening

- **Pin the finance agent's permissions as a subset of Haku's, then close the BuildBuddy gap.**
  Intent is in `cluster/docs/agent_rbac.md` (Haku directs the finance agent; the finance preset is a
  weaker subset with its own Forgejo identity). Kubernetes already holds at resolved-permission level
  (`test_agent_permission_superset_and_finance_parity`; adding `assert not uncovered(finance, haku)`
  there states it directly). Still to cover: the two presets' egress policies and action policy sets,
  compared by what they allow rather than by name. Known divergence: the finance preset has the
  `buildbuddy` egress policy and Haku does not; give it to Haku or drop it from finance. Also decide
  whether the finance preset's GitHub read action sets (public repos, `ducktape`, its fork,
  `gaffer-private`) stay, since both presets already hold the `github-agentydragon-agent` PAT policy.

- **Make the Claude web clone-complete check method-neutral.** `runtime/claude_web_env/bootstrap.sh`
  announces `~/haku-state/items` as the readiness signal and `run.md` waits on
  `~/haku-state/surface/items`. The atomic swap already makes the existence of `~/haku-state` the
  signal, so the path check only ties the wait to haku-state's current layout.
- **Verify the JWT mint** — confirm the `authentik-jwt-rotation` CronJob produces
  `secrets/haku-k8s-jwt.yaml` (the `client_credentials`-as-`haku-k8s` flow with
  `expected_group: haku`). The web home's whole kubectl path depends on it.
- **Haku LiteLLM key** — a key in `cluster/cdk8s/litellm/crossplane_keys.py` → a
  `haku-sandbox` secret for attribution / budget / kill-switch, if routing model calls
  through LiteLLM.
- **Tighten egress** — narrow the `haku-sandbox` CCNP `toEntities: cluster` to
  only Haku's named in-cluster sources (the gap `claude-sandbox` also accepts).
- **Haku's agentplane principal is named `claude-ai`.** With haku-console being decommissioned,
  Haku runs as a claude.ai/code agent whose agentplane-staging MCP connection authenticates as the
  `claude-ai` ServiceAccount, so Haku's credentials (`forgejo-haku`, `haku-mailbox`) and the reads
  it relies on are bound there (`cluster/cdk8s/agentplane/actions_staging_policies.py`). The name
  describes the client connection, not the agent: any other claude.ai/code agent run through the
  same connection is Haku as far as agentplane can tell. Not pressing while Haku is the only agent
  on that connection. When it matters: connect the same MCP server again as a separate claude.ai
  connection bound to its own ServiceAccount, one per agent, and move the Haku-owned bindings to
  Haku's.

## Console — operator-facing dashboard

The console design + action model live in `console/README.md`; the free-form-UI
contract in `console/docs/containment.md`. (The launch-routine button itself
has shipped on the capability tier — see the README.)

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
<runtime/x/managed_agent/README.md>):

- **Self-hosted worker (Runtime B)** — operator activation to go live:
  <runtime/x/managed_agent/self_hosted/README.md> and its bring-up RCA.
- **Anthropic-hosted cloud** — **PARKED (2026-07-04)**: the cloud control-plane
  objects were deleted at Anthropic and `cluster/parked/cloud-agent-tf` is
  suspended; see <runtime/x/managed_agent/anthropic_hosted/README.md> for the reason
  and the resume decision. Per-runtime TODO (mostly moot until resumed):
  <runtime/x/managed_agent/anthropic_hosted/TODO.md>.

## Later (post-v0)

- **In-cluster runtime** — realized as `runtime/x/agent_framework` (Runtime C, MAF
  self-hosted loop) and `runtime/x/managed_agent/self_hosted` (Runtime B, Managed
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
  (see <docs/agent_sdk_runtime.md>).
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
