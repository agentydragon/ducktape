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
  reflected into `haku-sandbox`. Remaining **Haku-side wiring (deferred)**:
  - Decide how Haku reaches it: an in-cluster MCP client / `kubectl
port-forward` from a sandbox pod using the reflected `haku-tana-ro-token`,
    vs. threading the Claude Code harness's MCP client to
    `tana-mcp-ro.tana-mcp.svc.cluster.local:8765/mcp` (needs the bearer wired
    through the web env alongside the SOPS→kubectl path). The user prefers
    starting simple (talk to it from a pod) over harness MCP wiring.
  - Cluster-internal reach is already permitted by the `haku-sandbox` CCNP
    `toEntities: cluster`; no new CNP needed for that hop.
  - Confirm the read-only allowlist against the live `tools/list`; settle the
    `get_or_create_calendar_node` exclusion (it can create a daily node).
  - Add a `tana` playbook — read recent daily/calendar notes + recently-edited
    nodes (synthesized from `search_nodes` + calendar-node traversal) → stale
    tasks, captured notes implying action — and a credentials-table row in
    `base/instructions.md`.
- **ducktape git history** — Haku already has the ducktape checkout and runs
  `git log` for base-sync; add a `ducktape_git_review` playbook that scans
  recent history (new TODO/FIXME, new `TODO.md`/`PLAN.md` entries,
  follow-up-flagged commits, reverts, half-finished threads) → proposals. No
  infra; playbook + `base/instructions.md` row only.
- **Cluster Forgejo repos** — read access to `ducktape` and `gaffer-private`
  if/when they're migrated or mirrored to the cluster Forgejo: grant the `haku`
  Forgejo user read, add a repo-activity playbook (open PRs/issues/review
  requests needing attention). `gaffer-private` stays private.
- **ActivityWatch** — read-only access to activity-tracking data once it's ready
  (currently suspended; see `cluster/` ActivityWatch). Useful for time-use
  patterns and "what changed in your routine" reasoning (e.g. cross-referencing
  CPAP weekend leakage with weekend activity).
- **Google Drive + Keep scopes** — add `drive.readonly`
  (+ `drive.activity.readonly`) and `keep.readonly` to the airlock
  `google-access-token` grant so the `drive_activity` / `keep_notes` playbooks
  work (today they 403 and log the gap). Other Google products (Docs, Tasks)
  light up the same way as their scopes are added.

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
