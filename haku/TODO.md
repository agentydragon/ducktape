# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done.

## Repo-boundary follow-ups (from the 2026-07-07 state_template retirement)

- **Shared haku-console client, if duplication bites:** both repos are bazelized, so
  haku-state could take a Forgejo-repo dependency on ducktape and consume a generic
  haku-console python/ts client (request/record models, the submit/sweep calls) instead of
  keeping its own copies in `ui/backend`/`ui/frontend`. Do it when the hand-rolled client
  drifts or a third consumer appears — not before.
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

- **Gmail labels (`haku/` namespace)** — **Built + deployed:** the `gmail-labeling` MCP
  server (sibling service, `tana-mcp-ro` shape) manages only labels whose name starts with
  `haku/`, enforced in-process (the closure invariant; contract in
  `haku/gmail_labeling/SPEC.md`). It's safe by construction, so there's no Airlock gate —
  rung 2 (filter the tool surface), not rung 3. Own namespace, bearer
  `haku-gmail-labeling-token` reflected into `haku-sandbox`, public bearer-gated route
  `gmail-labeling.allegedly.works`; the `gmail.modify` token is Airlock-provisioned (the
  `gmail_modify` OAuth provider) and ESO-mirrored **only** into the `gmail-labeling`
  namespace, never a sandbox — so Haku's own Google token stays read-only. Code:
  `haku/gmail_labeling/`; deploy: `cluster/k8s/agents/gmail-labeling/`.
  Remaining:
  - **One-time OAuth consent** for `gmail.modify` at
    `airlock.allegedly.works/oauth/authorize/gmail_modify` (operator). The pod waits on
    its token volume until this is done.
  - **Wire it into Haku and flip the doctrine:** add a `base/sources/gmail_labeling.md`
    (the route URL plus the bearer from the reflected secret), register it in the live
    runtime, and record in `base/instructions.md` and Haku's SPEC that Haku may now
    autonomously label within `haku/`. This is Haku's **first autonomous write** — do it
    only once the server is live and consented (otherwise Haku gets a source that 503s).

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
- **Anthropic-hosted cloud** — v0 known issues (token propagation, egress, the
  k8s-MCP path) + the Terraform-provider evaluation:
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
