# Haku TODO

Project-level TODOs for Haku. Design rationale lives in `PLAN.md`; this is the
actionable checklist. Remove entries once done.

## New read-only sources to wire

Each follows the same pattern: a read-only credential or filter facade reachable
from `haku-sandbox`, plus an example playbook in `base/playbooks/`.

- **CPAP data** — read-only access to daily summaries / AHI / compliance (see
  `cpap/`; WebDAV + EDF). Land scoped read creds as a `haku-sandbox` secret and
  add a `cpap` playbook (compliance dips, AHI spikes, mask-leak trends).
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

## Managed Agents runtime (Runtime B) — operator activation to go live

Runtime B (`runtime/managed_agent/self_hosted/`) is built end-to-end and its control plane is
provisioned (environment `env_015uqL9WAMSDytQEWWmLG9zF`, agent, vault +
`tana-mcp-ro` credential, scheduled deployment `depl_011DSrUoXuhoDWJoPyDuePqR`;
full IDs recorded on #2438). PR #2442 adds the worker image build+push and the
k8s manifests (`cluster/k8s/haku/agent-worker/`, shipped **suspended**). What
remains is operator activation (runbook in that dir's README):

- **Generate the environment key** in the Console (Environments →
  `haku-selfhosted` → "Generate environment key") and `sops`
  `cluster/k8s/haku/agent-worker/environment-key.sops.yaml` to the real value
  (placeholder today, encrypted to the cluster/Flux age key).
- **Activate + validate**: flip `suspend: false` on the Kustomization and watch
  the Deployment — first systemd-PID1 pod in the cluster, so confirm it boots
  unprivileged (cgroup-v2 delegation, writable `/run`) and tune the pod
  `securityContext` if needed.
- **Smoke test** — `ant beta:deployments run --deployment-id depl_011DSrUoXuhoDWJoPyDuePqR`,
  watch in the Console.

Settled (not blockers):

- **Image build + push** — `nix build .#haku-worker-image` (full-NixOS,
  `runtime/managed_agent/self_hosted/nixos.nix`) → `.github/workflows/haku-worker-image.yml`
  imports + pushes to `ghcr.io/agentydragon/haku-worker`; Flux tracks the tag via
  the `haku-worker` ImagePolicy.
- **Egress** — `api.anthropic.com` is on the `haku-mitmproxy` allowlist
  (`cluster/k8s/agents/haku-mitmproxy/cnp-haku-cloud-api-egress.yaml`); the worker
  reaches the work queue through the TLS-terminating proxy and trusts its CA via
  the inject policy (imported into the systemd unit).
- **SOPS identity** — the in-cluster worker needs no `SOPS_AGE_KEY`: it uses its
  `haku-worker` ServiceAccount for `kubectl` and reads creds from k8s secrets
  (only the web home decrypts the public-`kubeapi` JWT via SOPS).
- **Git sources** — haku-state on the in-cluster Forgejo
  (`git.allegedly.works/haku/haku-state`, single `.netrc` via `HAKU_GIT_HOST` +
  the `haku-state-git-write` creds); ducktape on public GitHub (anonymous,
  read-only) — it isn't mirrored to the cluster Forgejo yet (that migration is
  the "Cluster Forgejo repos" item above).

## Later (post-v0)

- **In-cluster runtime** — realized as `runtime/agent` (Runtime C, MAF
  self-hosted loop) and `runtime/managed_agent/self_hosted` (Runtime B, Managed Agents
  self-hosted worker; remaining wiring above). The old `haku-scanner` image +
  CronJob idea is superseded.
- **haku-traces** — push Claude Code transcripts to a store separate from
  `haku-state` for replayability.
- **tier-2 execution** — haku-owned execution behind stronger gating, only if
  handoff-via-prompt proves too slow for routine actions.
