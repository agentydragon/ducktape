# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done.

## New read-only sources to wire

Each follows the same pattern: a read-only credential or filter facade reachable
from `haku-sandbox`, plus an example playbook in `base/playbooks/`.

- **CPAP data** — read-only access to daily summaries / AHI / compliance (see
  `cpap/`; WebDAV + EDF). Land scoped read creds as a `haku-sandbox` secret and
  add a `cpap` playbook (compliance dips, AHI spikes, mask-leak trends).
- **Tana workspace** — read-only Tana access. The cluster-internal read-only
  facade is **built**: `tana-mcp-ro` (in the `tana-mcp` namespace) fronts the
  Tana MCP, exposes only read tools (default-deny allowlist via the generic
  facade's `MCP_FACADE_TOOLS__ALLOW`), injects the Tana PAT server-side so
  callers never see it, and is gated by a static bearer `haku-tana-ro-token`
  reflected into `haku-sandbox`, and the `tana_review` playbook + the
  `haku-tana-ro-token` credentials row are wired into `base/`. Remaining:
  - **Connection paved + verified:** a sandbox pod reaches
    `tana-mcp-ro.tana-mcp.svc.cluster.local:8765` directly (the `.svc.cluster.local`
    target is in the pod's injected `NO_PROXY`; the `haku-sandbox` CCNP already
    permits `toEntities: cluster`) — confirmed live from `claude-sandbox`:
    `GET /healthz` → 200, `POST /mcp` without the bearer → 401. The `tana_review`
    playbook carries a stdlib `python:3-slim` MCP-client recipe (no `pip` in the
    sandbox). Haku still needs one real run to confirm the **authenticated**
    `tools/list` (needs its reflected bearer) and record the recipe in `memory/`.
  - Confirm the read-only allowlist against the live `tools/list`; settle the
    `get_or_create_calendar_node` exclusion (it can create a daily node).
  - **Future (PLAN north star):** retire the pod dance by wiring Tana into the
    harness — expose `tana-mcp-ro` behind a bearer-gated route and give Haku
    `mcp__tana_ro__*` tools natively (the `kubectl-local` stdio-MCP pattern, but
    the facade is already HTTP so a `.mcp.json` `http` entry suffices), with the
    bearer threaded from the reflected secret. Then `tana_review` drops its
    connection section.
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
- **Grocy** — expiring / below-minimum stock, shopping-list suggestions.

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

## Later (post-v0)

- **In-cluster runtime** — a `haku-scanner` image + Job/CronJob as an
  alternative/complement to the Claude Code web home.
- **haku-traces** — push Claude Code transcripts to a store separate from
  `haku-state` for replayability.
- **tier-2 execution** — haku-owned execution behind stronger gating, only if
  handoff-via-prompt proves too slow for routine actions.
