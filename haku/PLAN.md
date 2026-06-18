# Haku — Personal Background Agent

Status: building. The infrastructure is largely landed (scoped k8s identity + JWT
rotation, the `haku-sandbox` compute sandbox, Plaid read-only reflection, the
`haku-state` repo, the read-only base manual); the v0 Claude Code web-home runtime
is in flight (`haku/claude_web_env/`). Design rationale below; the **actionable
checklist is `TODO.md`**.

Named after Spirited Away's Haku: a dragon who quietly helps run the
household (and needs his name kept in writing — hence the repo).

## Goal

Haku runs in the background of my life with a bundle of (mostly
read-only) access tokens and a container, continuously looking for useful
things to do across everything it can see — Gmail, Calendar, Grocy, PostScanMail,
Plaid, Tana, Manifold, the cluster. It:

- acts **autonomously where safe and read-only** (scanning, summarizing,
  cross-referencing);
- produces **items** — concise, value-ranked suggestions — into a queue;
- I review the queue on a **dashboard** sorted by descending value (curated by
  an agent, not just raw output);
- approving an item means **handing it off**: the item carries a prepared
  prompt I take as a URL into an existing big scaffold (Claude app, Claude
  Code) that does the work under its own permissions. Haku executing
  things itself is a later maybe, not the MVP.

## What already exists — and what we deliberately don't build on

The data plane is built: MCP servers for every source, auth solved.
**Non-goal: building on the in-repo agent Python scaffolding** —
`agent_cli/`, `x/agent_server/`, `agent_pkg/`, compositor wiring. Claude Code
is the agent runtime; it mounts multiple MCP servers natively and brings
skills, sub-agents, and permission gating for free. The new work is an item
store, a dashboard, and prompts/skills.

| Need                    | Existing component                                                                                                                                          |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deployed data sources   | `cluster/k8s/agents/{google-workspace-mcp,postscanmail-mcp,manifold-mcp,tana-mcp,plaid-db-mcp}`, `cluster/k8s/grocy/*/mcp/`                                 |
| Auth for headless agent | `mcp_infra/oauth_facade/` (Authentik OIDC); client_credentials for non-interactive sessions                                                                 |
| Agent runtime           | Claude Code — headless `claude -p`, skills, native multi-MCP config, per-tool permission rules                                                              |
| Auditable data repo     | `tf/gitops/augur-evidence` — Forgejo repo + service users + k8s git-credential secrets, all Terraform-managed                                               |
| LLM routing + tracing   | `cluster/k8s/{litellm,langfuse}/` deployed; `tf/gitops/litellm-api-key` mints scoped keys                                                                   |
| Egress + tool gating    | `claude-sandbox` mitmproxy egress pattern (reuse); a read-only MCP filter proxy is the **required** boundary (to build); `airlock/` for per-call HITL later |
| Prior thinking          | `x/agent_server/docs/vision.md`, `docs/self_reflective_runtime/architecture.md` — direction we keep, code we don't reuse                                    |

Related prior notes: <../cluster/docs/plans/kagent_persistent_agents.md>
(persistent in-cluster agents), <../idea/timekpr_llm_guardrail.md> (LLM as
exoself), `x/fancy_terminal/ai_suggest_daemon_spec.md` (background suggestion
daemon UX).

## Architecture

Four roles, deliberately decoupled so each can be dumb at first:

```
                 ┌──────────────────────────────────────────────┐
                 │  read-only MCP fleet (gmail, calendar, grocy,│
                 │  postscanmail, plaid-db, tana, manifold, k8s)│
                 └──────▲───────────────────────────▲───────────┘
                        │ read-only creds           │ its own creds
   cron/event   ┌───────┴────────┐          ┌───────┴────────┐
   trigger ────▶│  SCANNER agent │          │ EXECUTOR: big  │◀── handoff URL
                │  (container)   │          │ scaffold, e.g. │
                └───────┬────────┘          │ Claude app     │
                        │ propose items     └───────▲────────┘
                        ▼                           │
                ┌─────────────────┐  decide/feedback│
                │   ITEM STORE    │◀────────────────┤
                │  (Forgejo repo) │        ┌────────┴───────┐
                └───────▲─────────┘        │   DASHBOARD    │
                        │ dedup/rerank/    │ (value-sorted) │
                ┌───────┴────────┐         └────────────────┘
                │ CURATOR agent  │
                └────────────────┘
```

- **Scanner** — runs on a schedule (later: on events) in a container with the
  read-only credential bundle mounted. Walks per-domain playbooks (skills) and
  proposes items via an `items_propose` MCP tool. Never executes anything.
- **Item store** — a private **Forgejo repo** (`haku-items`), not a database.
  One structured YAML file per item (schema below); every proposal, re-rank,
  and decision is a commit, so git history _is_ the audit log — who (which
  agent account or me) changed what, when, and why, with diffs. Provisioned by
  `tf/gitops/haku-items` (the `augur-evidence` pattern): the `haku` service
  user owns it, I review as Forgejo site-admin.
- **Dashboard** — v0 is **just a view of git**. Starts as the
  curator-maintained, value-sorted `items.md` rendered by Forgejo (zero UI
  code), later a committed static page served via git-sync (see phase 2).
  Exactly three affordances per item,
  all of which are commits: **hand off** (follow the handoff URL, mark
  `in_progress`), **archive** (flip to a terminal status), and **leave
  feedback** (write an intake entry referencing the item).
- **Intake** — a freeform back-channel in the same repo: I commit messages
  ("hey Haku, this was a great suggestion", "no email reminders until next
  week") via the dashboard's feedback button, Forgejo's web editor, or any
  git client. Haku consumes intake at the start of each run; see below.
- **Curator** — agent pass over the open queue: dedup against existing items
  (including previously rejected ones — don't re-nag), merge related items,
  re-score, expire stale/perished items. Initially can be the tail end of the
  scanner run rather than a separate process.
- **Executor** — **not part of this system.** Approved items are handed off as
  URLs into existing big scaffolds (Claude app, Claude Code session) that run
  under their own credentials and permission prompting. Haku never
  executes anything in the MVP; haku-owned execution (tier 2 below) is a
  later maybe.

### Item model

```python
class Item(BaseModel):
    id: str                      # ULID
    dedup_key: str               # stable key for "same suggestion" (e.g. "grocy:expiring:milk:2026-06-15")
    title: str                   # one line, dashboard row
    body: str                    # short rationale + evidence links (gmail thread ids, grocy product ids)
    value: int                   # 0–100 expected-usefulness score (curator-owned)
    deadline: datetime | None    # perishability; past deadline → auto-expire
    action: Suggestion | PreparedPrompt | ToolCalls   # discriminated union, see tiers
    source: str                  # scanner playbook that produced it ("gmail_triage", "grocy_stock")
    status: Literal["open", "approved", "in_progress", "done", "rejected", "snoozed", "expired"]
    rejection_reason: str | None # fed back into scanner context so it learns
```

Serialization: `items/<id>.yaml` in the `haku-items` repo, validated against
this schema by the proposing tool (and by CI in that repo). Items stay
machine-readable tables, not freeform notes — the curator regenerates the
rendered `items.md` view from them; freeform reasoning lives in `body` and
commit messages.

Action tiers (the union above):

- **Tier 0 — `Suggestion`**: pure FYI / "you should do X yourself". Approve =
  acknowledge.
- **Tier 1 — `PreparedPrompt`**: the workhorse. Full prompt text embedding the
  evidence and desired outcome so the executor session needs no archaeology.
  Rendered in `items.md` as a **handoff URL** — a `claude.ai/new?q=<prompt>`
  deep link where the prompt fits in a URL, otherwise a link to the raw item
  file to copy from.
- **Tier 2 — `ToolCalls`** _(later, maybe — explicitly not in the MVP)_:
  structured MCP calls (e.g. `grocy_shopping_list_items_add(...)`) that a
  small reactor would execute verbatim with elevated credentials on approval.
  Designed so the approval commit is the human gate and what runs is exactly
  what was reviewed; built only if handoff via prompts proves too slow for
  routine actions.

### Intake and steering

`intake/` holds freeform notes from me — praise, complaints, scoped snoozes,
standing instructions. Entries may reference an item id (the dashboard's
feedback button does this) or be general. At the start of each run Haku:

1. reads unprocessed intake entries,
2. distills them into `steering.yaml` — explicit rules with optional expiry
   (e.g. "no `gmail_triage` reminder items until 2026-06-19") plus
   per-playbook reinforcement notes,
3. moves each processed entry to `intake/processed/` with its interpretation
   appended, so I can audit how my words got translated into rules.

`steering.yaml` + recent rejection feedback are injected into scanner and
curator context on every run. This is the primary tuning loop: I steer
Haku the way I'd steer a person — by telling it things — and the repo keeps
the full record of what I said and what it did with that.

### Haku's log (long-lived memory)

`log/` is Haku's own journal: run summaries, observations, a world
model, threads it's watching ("rent question pending since May"), hypotheses
about my preferences it hasn't yet confirmed via intake. It is
**self-structured** — Haku organizes it however serves it best. The
only constraints: plain markdown/YAML (diffable, auditable), it lives in the
autonomous write area, and Haku compacts it itself when it grows stale
(a periodic curator chore). Read at the start of every run, this is what
makes run N+1 smarter than run N without any database.

### Base vs. state

Haku is split into two repos along the code/data line, and **Haku's git
credential can write only the state repo**:

- **base** — `haku/base/` in ducktape: Haku's _instructions and config_.
  `AGENTS.md` (scan procedure + playbooks), `schema/item.json`, `.mcp.json`,
  the capability registry, `README.md`. Authoritative, versioned and CI'd in
  the monorepo, **baked into the scanner image** and read-only at run time —
  Haku never writes it.
- **state** — the `haku-items` repo: Haku's _accumulated information_. `items/`,
  `intake/` (+ `processed/`), `steering.yaml`, `log/`, generated `items.md`.
  This is the **only** thing Haku writes, and the only place it can.

```
haku/base/  (baked into image, read-only)    haku-items/  (state, Haku writes)
├── CLAUDE.md          # @AGENTS.md           ├── README.md      # what this repo is
├── AGENTS.md          # operating manual     ├── items/<id>.yaml
├── .mcp.json          # MCP server set       ├── items.md       # value-sorted view
├── schema/item.json   # JSON Schema          ├── intake/        # freeform notes from me
└── registry.yaml      # service→facade→sec    │   └── processed/ # + Haku's interpretation
                                              ├── steering.yaml  # distilled rules
                                              └── log/           # self-structured memory
```

Properties this buys:

- **The scan job is generic**: base is the image's working directory (it
  carries `AGENTS.md`/`.mcp.json`/`.claude/`); the entrypoint clones only state,
  runs Claude Code, "follow `AGENTS.md`," writing only into the state clone.
  Behavior is base content, not job config.
- **Self-modification is structural, not policed by CI.** Haku _cannot_ edit
  its own instructions or schema in a run — they live in a repo it has no write
  credential for. Changing them is a **PR against ducktape**, gated by the
  monorepo's existing review + CI. This replaces the old single-repo
  path-scoped-write / protected-files / auto-merge apparatus entirely: there is
  nothing gated _inside_ state, so state needs no gate. `steering.yaml` is the
  deliberate tunable that lives in state — policy data Haku maintains from
  intake, not instructions.
- **base is versioned with the image, not a stale seed.** Decided: base is
  **baked into the `haku-scanner` image**. A playbook edit in ducktape isn't a
  manual reseed — it rebuilds the image, and Flux image automation bumps the
  CronJob's pinned tag (the standard <../cluster/docs/container-images.md>
  flow), so the next run picks it up. The cost vs. cloning is an image build in
  the path; the win is no extra repo, credential, or monorepo read grant. state
  is seeded once: Terraform creates `haku-items` (`auto_init`) and a one-shot
  push lays down the empty skeleton (`steering.yaml`, dirs, a `README.md`
  pointing at base).
- **Schema lives in base** (`schema/item.json`, exported from the ducktape
  `Item` model via the `airlock/export_schema.py` pattern). Haku validates
  items at write time against the baked-in schema; ducktape CI validates base.
  No second copy in state to drift.

### Identity: Haku is its own principal

Haku authenticates as _itself_ everywhere — it never borrows my logged-in
session. Each credential is issued to a Haku-scoped identity, which buys three
things: **attribution** (every action shows up as Haku's in the service's own
logs), **independent revocation** (kill Haku without touching my own access),
and a **blast radius** bounded by what Haku was granted, not by everything I
can reach. Two classes:

- **Resources Haku owns outright** — created for Haku, holding only Haku's data:
  - `haku` Forgejo user — owns `haku-items` (its queue, intake, steering, log).
    Already provisioned in `tf/gitops/haku-items`.
  - `haku` Authentik service account — its identity at every MCP facade
    (client*credentials; see \_MCP auth provisioning*).
  - Haku LiteLLM virtual key — per-Haku attribution, budget, kill switch.
  - `haku-traces` store and a `haku` ntfy topic.
- **Resources Haku is _granted access to_ but I own** — Gmail, Calendar,
  Grocy, Plaid, PostScanMail, Tana, Manifold. Haku never holds the upstream
  credential for these. They sit behind Authentik-fronted MCP facades; Haku
  presents _its own_ service-account token to the facade, and the facade holds
  the shared upstream OAuth/API credential. So "Haku's own Gmail credential" is
  its facade token, not a Google grant — Haku reads my mail _through_ the
  facade but cannot see, export, or outlive my Google login, and revoking the
  service account cuts all of it at once.

The single deliberate exception is the v0 laptop bootstrap (below), which
borrows my interactive MCP OAuth sessions — explicitly temporary, ending at
the in-cluster move.

### Access model: the container _is_ the trust boundary

Assume everything mounted into or reachable from Haku's container is **fully
available to the agent** — every tool on a reachable MCP server, every
credential in the environment, the full capability of any mounted token. A
prompt-injected instruction in an email body, a scanned mail item, or a
transaction memo is assumed able to invoke anything Haku _can_ invoke. So the
boundary is enforced **outside the agent**, at the credential / network /
proxy perimeter — **never** by instructions in `AGENTS.md` or Claude Code
permission rules. Because of that, the in-cluster scanner runs
`claude --dangerously-skip-permissions` (Bash and every tool auto-allowed) with
no Claude Code bash sandbox: in-agent gating is redundant friction, and the Pod
(mitmproxy egress + read-only creds + scoped RBAC) is the real fence. (This is
the Ember posture: capability-of-the-container = capability-of-the-agent.) The
Pod runs as non-root so `--dangerously-skip-permissions` is accepted.

Read-only is achieved by construction, three ways, in order of preference:

1. **Scope the upstream credential** so it _cannot_ write — the preferred
   method whenever the source supports it. **Plaid is the clean case**: the
   `plaid-mcp-db` CNPG cluster already defines a read-only managed role
   (`plaid_ro`), so Haku gets its own read-only role and queries it with `psql`
   — the credential is incapable of writes and no MCP server or proxy is involved.
   **Gmail + Calendar are the same shape**: airlock's `google` OAuth grant uses
   all-`.readonly` scopes, so its access token (mirrored into `haku`) can only
   read — Haku calls the REST APIs with it directly (see _Google_ below).
   Likewise read-only service users wherever a source offers them.
2. **Filter the tool surface** with a read-only MCP facade — an allowlist
   proxy that exposes only the read subset, so write tools are not _on the
   wire_ Haku sees. This is the mechanism for HTTP MCP servers that have **no
   read-only-credential trick**: PostScanMail, Gmail, Calendar, Grocy, Tana.
   Airlock is the heavier per-call-gated variant; the MVP needs only a static
   allowlist filter. **The Authentik OAuth facade is _auth_, not tool
   filtering — it forwards the full tool set**, so a separate read-only layer
   is required in front of it.
3. **Lock egress at L3/L4 _and_ L7.** A CiliumClusterwideNetworkPolicy on the
   `haku-sandbox` namespace forces all workload egress through a **dedicated
   `haku-mitmproxy`** (its own CA + FQDN allowlist, modelled on the shared
   `agents-mitmproxy` that `claude-sandbox` uses but isolated to Haku): a
   TLS-terminating proxy that domain-allowlists outbound traffic and logs every
   request/response. `haku-sandbox` gets it via the proxy's CA-cert reflection,
   an `inject-haku-mitmproxy` Kyverno policy, and a
   `haku-sandbox-force-proxy-egress` CCNP; the proxy's `cnp-haku-cloud-api-egress`
   allowlists the upstreams (container registries + Gmail/Calendar Google APIs).
   Cluster-internal targets (LiteLLM, Forgejo, Plaid Postgres) are reached
   directly, not through the proxy. L7 allowlisting catches what the L3 policy
   can't express (path-level rules, full payload logging for audit) and yields
   a complete on-the-wire record of everything Haku does.

Two further perimeter facts:

- **The only write capability Haku holds is the state repo.** The mounted git
  credential grants write to `haku-items` and nothing else. Its instructions
  and schema live in **base** (ducktape, cloned read-only), a repo Haku has no
  write credential for — so self-modification is a PR against ducktape, gated
  structurally rather than by branch-protection rules inside the state repo
  (see _Base vs. state_). v0 can push state straight to `main`.
- **No elevated credentials exist anywhere in the MVP.** Execution happens in
  handoff-target scaffolds under _their_ own credentials and prompting. If
  tier 2 is ever built, its credentials get the same treatment — scoped or
  proxied, never trusted to the agent's restraint.

## Deployment and access provisioning

### Runtime environment and the capability registry

Haku runs as a **CronJob in a `haku-sandbox` namespace** under its own
**ServiceAccount**. Credentials are not baked into the image or pre-mounted as
a fixed env bundle — Haku **provisions them itself from k8s at runtime**. The
model: tell Haku "to use service X, call facade Y (in-cluster URL) with the
credential in secret Z," and let it `kubectl get secret` what it needs.

- **`haku-sandbox` is a claude-sandbox-style sandbox.** The CronJob's
  ServiceAccount and the OIDC group `oidc-ksbx-groups:haku` get a Role with full
  CRUD **inside the `haku-sandbox` namespace** (pods/jobs/configmaps/secrets/…),
  so Haku can run workloads and read the creds mounted to it — but **nothing
  outside the namespace** (deliberately _not_ the broad `kubectl-sandbox-users`
  diagnostics group). The perimeter is structural: namespace-scoped RBAC, the
  dedicated mitmproxy egress allowlist, and a ResourceQuota/LimitRange — none
  relying on the agent's restraint. The Ember invariant still holds: Haku can
  fully use any secret it can read, so **every credential reflected into
  `haku-sandbox` must be read-only/scoped** (the `plaid_ro` DSN, the read-only
  Google token, Haku's own git / LLM keys) — never a write-capable upstream token.
- **Capability registry.** A ConfigMap (or a section of `AGENTS.md`) maps
  `service → facade URL → secret name`. Haku consults it, reads the named
  secret, connects to the facade. Granting Haku a new source is then a
  **pure-k8s operation**: deploy the read-only facade, write its credential to
  a `haku-sandbox` namespace secret, open a NetworkPolicy egress to it, add a registry
  row. No image rebuild, no redeploy — the next run discovers it.
- **Own keys live the same way** (see _Identity_): the `haku-items` git creds
  (Terraform → k8s Secret, `augur-evidence` pattern) and the Haku LiteLLM key
  are `haku-sandbox` namespace secrets, read the same way. SOPS-in-repo + Reflector
  remains the mechanism for any secret originating outside Terraform.

The boundary stays entirely in k8s: **RBAC** says which secrets, **NetworkPolicy**
says which facades, the **facades** say which tools — none of it relying on the
agent's restraint.

### LLM routing and traces

Record everything; it's all attributable infrastructure that already runs:

- **All LLM traffic through in-cluster LiteLLM** (`ANTHROPIC_BASE_URL` →
  `litellm.allegedly.works`). A Haku-scoped virtual key (Terraform module
  like `tf/gitops/litellm-api-key`) gives attribution, budgets,
  and a kill switch.
- **LiteLLM → Langfuse callback** for full request/response traces, tagged
  with haku + run id. Langfuse is already deployed
  (`cluster/k8s/langfuse/`, SSO via `sso-providers/provider_langfuse.tf`).
- **Claude Code session transcripts** (JSONL) pushed at the end of each run
  to a traces store **separate from `haku-items`** (a `haku-traces` repo
  or bucket) — full replayability without diluting the audited decision
  record.
- Pod logs flow through the cluster's standard logging stack.

This works from phase 0 — routing through LiteLLM is just env vars.

### MCP auth provisioning (decided: client_credentials)

The in-cluster MCP servers sit behind Authentik OAuth facades built for
interactive flows (DCR + PKCE). Direct in-cluster access bypassing the
facades is **foreclosed by the current posture** — each MCP Service exposes
only the facade port (e.g. `plaid-db-mcp:8765`) and CiliumNetworkPolicy
admits only Gateway ingress; the upstream MCP container is loopback-only.
Weakening that to let Haku in the back door isn't worth it.

So: a `haku` Authentik **service account + client_credentials** (pattern:
`sso-providers/service_account_claude.tf`) with grants on each MCP app. The
scan job's entrypoint exchanges credentials for per-facade JWTs — working
in-repo example: `cluster/k8s/agents/authentik-jwt-rotation/rotate.py` — and
`.mcp.json` injects them via `${ENV}` expansion in `headers`. Gives Haku a
real identity in every MCP server's logs. Remaining spike: verify a facade
accepts service-account-issued JWTs (group-claim checks; see
`cluster/docs/mcp_oauth_authentik_notes.md` for the prior saga) — test
against the plaid-db facade.

Reconciliation with the Ember boundary: the Authentik facade is _auth_, not
tool filtering, so it isn't the read-only boundary — a read-only filter proxy
sits in front of it (see _Access model_). The client*credentials can live
**in that filter proxy** (cleanest: the proxy authenticates upstream and Haku
holds only a simple bearer token to the proxy, read from a `haku-sandbox` namespace
secret), or Haku can hold them and authenticate to the auth facade \_behind*
the filter. Settle this when the proxy is built; either way the credential is
a `haku-sandbox` namespace k8s secret and the connection runs through the mitmproxy
egress. The client_credentials spike is still needed for whichever component
authenticates to the Authentik facade.

### Alternative runtime: Anthropic Managed Agents (buy instead of build)

Anthropic's Managed Agents (beta) is essentially phase 1 as a service: a
persisted agent config (model/system/tools/MCP servers), per-session
containers, **scheduled deployments** (cron-fired sessions — our CronJob),
and **vaults** (Anthropic-held MCP credentials with OAuth auto-refresh,
injected at egress, never visible in the sandbox — our headless-MCP-auth
spike). The scan job would be a deployment whose `initial_events` say "do a
scan per AGENTS.md", with the repo cloned via a vault-held git token.

Costs vs. the self-hosted path: the agent loop, event history, and traces
live at Anthropic (no LiteLLM/Langfuse attribution, no `haku-traces`);
in cloud mode the execution container does too. The middle option,
**self-hosted sandboxes**, keeps tool execution + filesystem + egress in the
`haku-sandbox` namespace via an outbound-polling worker while Anthropic runs the
loop. The repo mount is GitHub-only, so Forgejo would be cloned via bash +
vault env-var credential either way.

Verdict: not the plan of record — the Claude Code CronJob keeps everything
self-hosted and is fully specced above — but revisit if the
client_credentials spike or scanner-image upkeep proves painful; scheduled
deployments + vaults remove exactly those two work items.

## MVP plan

Optimize for a working loop in days, iterate from there.

### Phase 0 / v0 — the sandboxed in-cluster scanner

v0 _is_ the Ember-compliant `haku-sandbox` namespace, not a laptop command (decided
2026-06-13). The "do the playbooks produce items I actually want?" question is
answered _inside_ the sandbox: run the Job by hand and iterate `AGENTS.md`
until the items are good, then flip the trigger to a CronJob. Build order, each
step a discrete deliverable:

1. **Forgejo repo + service user** — `tf/gitops/haku-items` (DONE): the `haku`
   user owns the private `haku-items` repo, git creds in a `haku-sandbox` namespace
   Secret; I'm Forgejo site-admin so I review without an explicit grant.
2. **base + state skeleton** authored (DONE — split into `haku/base/` +
   `haku/state_seed/`; see _Base vs. state_): base carries `AGENTS.md` +
   playbooks, `schema/item.json`, and an empty `.mcp.json` (Plaid is plain
   `psql`, not an MCP server — see step 6); the state seed is the empty
   `steering.yaml`, dirs, and a placeholder `items.md` pushed once into
   `haku-items`. No `.claude/` permission config — the run bypasses permissions
   (step 8). Remaining: a focused pass on the operating manual + playbooks as
   sources come online.
3. **`haku-scanner` image** — node + Claude Code + git + kubectl + `psql`
   (postgresql-client) + `curl` + base baked in, published to GHCR via the
   <../cluster/docs/container-images.md> path. Runs as a **non-root** user (so
   `--dangerously-skip-permissions` is accepted). No Claude Code image exists in
   the cluster yet; this is the one real build artifact.
4. **`haku-sandbox` namespace + ServiceAccount + sandbox RBAC + quota** — the
   namespace + `haku-scanner` SA + scoped JWT identity (group `haku`) landed via
   the identity PR (#2354, merged). The haku-sandbox PR renames the namespace
   `haku`→`haku-sandbox` and upgrades RBAC to a claude-sandbox-style
   `haku-sandbox-admin` Role (full CRUD _within the namespace_:
   pods/jobs/configmaps/secrets/…) bound to the SA + group, plus a ResourceQuota
   and LimitRange. Full in-namespace compute, nothing outside.
5. **Egress sandbox** — the haku-sandbox PR gives Haku a **dedicated
   `haku-mitmproxy`** (its own CA + FQDN allowlist), not the shared instance:
   the CA-cert reflects into `haku-sandbox`, an `inject-haku-mitmproxy` Kyverno
   policy sets HTTP(S)\_PROXY + mounts the CA, and a
   `haku-sandbox-force-proxy-egress` CCNP forces egress to DNS, `toEntities:
cluster` (covers `plaid-mcp-db-ro:5432`, in-cluster LiteLLM, and
   `forgejo-http`), kube-apiserver:6443 (for `kubectl get secret`), and the
   proxy:8080 for external HTTP. The proxy's `cnp-haku-cloud-api-egress`
   allowlists the upstreams (container registries + Gmail/Calendar Google APIs).
   No new Plaid ingress needed — the CNPG db pods carry no ingress policy.
   (Possible later hardening: narrow `toEntities: cluster` to only Haku's named
   cluster sources — the same gap `claude-sandbox` accepts.)
6. **First source — Plaid via the read-only CNPG role (no proxy).** _Reflected
   secret DONE_ (`cluster/k8s/agents/plaid-mcp/db`): rather than a dedicated
   role, the existing `plaid_ro` role's ESO secret `plaid-mcp-db-readonly` is
   reflected (emberstack) into the `haku-sandbox` namespace — Haku shares the MCP
   facade's read-only credential (revoke by rotating that one). Remaining: a
   CiliumNetworkPolicy egress (`haku` → `plaid-mcp-db-ro.plaid-mcp:5432`) +
   matching ingress, landing with the scanner workload; then Haku just runs
   **`psql`** against the reflected DSN — no MCP server. The read-only role _is_
   the boundary — `psql` can only `SELECT` — so Plaid needs **no MCP server, no
   filter proxy, and no Authentik/JWT spike** (Postgres auth over raw TCP, so the
   mitmproxy isn't in this path either). The `haku-scanner` image just needs
   `psql`. Registry row: `plaid → psql, read-only DSN in secret
plaid-mcp-db-readonly`.
7. **Haku LiteLLM key** — `tf/gitops/litellm-api-key` → a `haku-sandbox` namespace
   Secret; LiteLLM → Langfuse gives traces from the first run.
8. **Job, then CronJob** — runs as the `haku-scanner` SA (non-root). The
   entrypoint exports the Plaid read-only DSN from the `plaid-mcp-db-readonly` secret
   (env / `.pgpass`), clones `haku-items` to `./state`, and runs
   `claude -p --dangerously-skip-permissions "Do a scan per AGENTS.md"`, then
   pushes state. Permissions are bypassed and Claude Code's bash sandbox is off
   — the Pod perimeter is the boundary (see _Access model_). Trigger by hand
   while tuning playbooks; flip to a CronJob once the items are worth automating.

Intake works from day one — I commit to `intake/` via the Forgejo web editor
(phone included); the scan includes unprocessed intake verbatim, with
distillation into `steering.yaml` deferred to phase 1. Review and approval stay
in Forgejo: the rendered `items.md` is the dashboard, approval = following an
item's handoff URL.

**The second source proves the registry _and_ the filter-proxy path.** Plaid
used the scoped-credential trick; PostScanMail is the first HTTP MCP with no
read-only role, so it gets the read-only filter facade — and adding it should
be pure k8s: stand up the filter facade, write its secret, open a
NetworkPolicy egress, add a registry row, no image rebuild. This is where the
Authentik client_credentials work lands — the **open impl choice** (creds in
the proxy so Haku holds only a bearer, vs. in Haku) and the "does a facade
accept service-account JWTs" spike. If it holds, both the "add a source = k8s
config" model and the filter-proxy mechanism are proven.

v0 playbooks (all read-only by construction, no facade): `plaid_anomalies`
(unusual charges, forgotten recurring payments, fees) over `psql`;
`gmail_triage` (threads awaiting a reply, buried deadlines, killable
subscriptions) and `calendar_prep` (events missing prep/travel, conflicts) over
the Gmail/Calendar REST APIs with airlock's read-only Google token (see
_Google_ below). Still waiting on a read-only filter facade (phase 1):
`postscanmail` (unopened mail → open/discard) and `grocy_stock`
(expiring/below-min stock); PostScanMail is the first facade and proves the
"add a source = pure k8s" + filter-proxy path.

### Google (Gmail + Calendar) — airlock's read-only token

A third "scope the upstream credential" case, like Plaid. Airlock's `google`
OAuth provider is configured with **all-`.readonly` scopes** (`gmail.readonly`,
`calendar.readonly`, …) and stores the refreshed access token in the
`google-access-token` Secret in the `airlock` namespace. A `ClusterExternalSecret`
(`cluster/k8s/agents/airlock/eso-access-tokens.yaml`) mirrors it into `haku`
(refreshed every 1m, tracking airlock's refresh); Haku reads `access_token`
from it and calls the Gmail/Calendar REST APIs with a Bearer header. The token
is structurally read-only, so no MCP server and no filter facade are needed —
only the refreshed _access_ token is mirrored (not the refresh token in
`google-tokens`), bounding blast radius. The mitmproxy allowlist
(`cnp-cloud-api-egress.yaml`) gains `gmail.googleapis.com` + `www.googleapis.com`
so the proxied `curl` reaches Google. DONE.

### Phase 1 — curation structure + more sources

Haku already runs unattended in the sandbox, so phase 1 widens and sharpens
the queue rather than getting Haku to run at all:

- **More sources, each a pure-k8s addition** (the registry model): Gmail,
  Calendar, Grocy, Tana — a read-only filter facade + secret + NetworkPolicy
  egress + registry row apiece, no image change. Playbooks already designed.
- **Intake distillation**: Haku maintains `steering.yaml` from intake entries
  and per-item feedback instead of re-reading raw intake every run.
- **Self-modification is already structural** via the base/state split — base
  (instructions/schema) lives in ducktape and Haku can't write it, so there's
  no in-repo path-scoped gate to build. Optional later: route state writes
  through branch PRs for a review surface, but direct pushes to state are fine
  — nothing gated lives there.
- **(Optional) `items` MCP server** wrapping the repo (`items_propose` /
  `items_list` / `items_decide`) — build only if direct file edits prove
  error-prone; schema CI is the check until then.
- **Repo CI** from the template: schema validation + the
  agent-can't-edit-its-own-instructions rule.

### Phase 2 — handoff polish (+ optional UI)

- Better handoff: `claude.ai/new?q=` deep links in `items.md` where prompts
  fit; for longer prompts, a stable per-item URL whose content is
  one-click-copyable. Closing the loop stays manual at first (I flip
  `status` after the handoff session finishes).
- Dashboard ladder — **v0 is decided: the Forgejo web editor**, rendered
  `items.md` as the view. Escalate a rung only when the current one
  demonstrably hurts:
  1. **Committed static page**: haku-regenerated `dashboard/index.html` in
     the repo, served by a static container + git-sync sidecar behind
     Authentik (the augur evidence-sync pattern). Affordances are plain
     links: handoff URLs, Forgejo web-editor deep links for archive/feedback.
  2. **Pretty dashboard, still no backend**: a real web app (Svelte) whose
     build reads the repo from a git-sync volume and renders client-side;
     archive/feedback buttons call the Forgejo API with my Authentik session.
  3. **Proper app image**: Bazel `oci_image` → `push-images.yml` → Flux image
     automation (the standard <../cluster/docs/container-images.md> path),
     deployed in the `haku-sandbox` namespace, reading the repo via git-sync or
     Forgejo API. Only if rung 2's client-side-only model runs out of road.

### Phase 3 — later, maybe

- Push notifications: a Haku ntfy topic (creds reflected into the
  namespace — same plumbing as Flux's on-call alerts in
  `cluster/k8s/flux-webhook/ntfy-alerts.yaml`) for time-sensitive or
  delightful FYIs — "hey, there's a concert tomorrow you might like". This is
  Haku's first write capability outside the repo, so it's governed:
  thresholds and budget (min value, deadline proximity, quiet hours, daily
  cap) live in `steering.yaml`, and every push references an item so the
  repo stays the complete record.
- Discovery playbooks beyond my own data: local events/concerts matching my
  taste, deals on standing wishlist items — needs web search access in the
  scanner.
- Haku-owned tier-2 execution: a small reactor triggered by Forgejo webhook
  on `status: approved` commits replays the stored tool calls with elevated
  credentials and commits the result back to the item.
- Event triggers instead of cron: Gmail push notifications, Grocy
  `get_db_changed_time` polling, calendar webhooks — per the hook model in
  <../docs/self_reflective_runtime/architecture.md>.
- Graduated autonomy: per-playbook policies that auto-approve proven-safe
  tier-2 actions (agent proposes policy expansions, I approve the _policy_,
  per `x/agent_server/docs/vision.md`).
- Multiple specialized scanners with per-domain credential bundles.
- Self-provisioned services via Flux: point a Kustomization at a
  haku-owned manifests directory, reconciled into the `haku-sandbox` namespace —
  Haku stands up its own services by committing manifests, bounded by
  namespace RBAC + ResourceQuota, revocable by suspending the Kustomization.
  Manifest paths start PR-gated; subtrees can graduate to the autonomous
  list as trust grows — infra authority is just another path in the
  write-authority model.

## Open questions

- **Does tier 2 ever get built, and does it need a per-call gate (airlock)?**
  Deferred until handoff-via-prompt proves too slow for routine actions. If
  built: item-level approval of verbatim tool calls is the gate; airlock only
  earns its place if execution ever becomes agent-mediated (where what runs
  can diverge from what was reviewed).
- **Value scoring**: single curator-owned 0–100 plus deadline is probably
  enough for the MVP; resist building an expected-utility framework before the
  queue has real traffic.
- **Notification thresholds**: ntfy is the channel (what Flux alerts already
  use); when to ping vs. wait for a dashboard visit is a `steering.yaml`
  matter — tuned via intake like everything else. Matrix is the richer later
  option if notifications ever grow replies.
- **MCP auth for the headless Haku**: decided — client_credentials (see "MCP
  auth provisioning"). Remaining spike: confirm the facade JWT verification
  accepts service-account-issued tokens, against the plaid-db facade.
- **Git as item store at scale**: a repo gives auditability, trivial backup,
  and human-editable state, but no queries or concurrent-writer safety. Fine
  at personal item volumes with effectively serialized writers (CronJob
  scanner, one human). If item volume or writer concurrency ever outgrows it,
  add a read index (the repo stays the source of truth) rather than moving
  authority to a DB.
- **Tighten the shared groups scope mapping** (deferred hardening): the Haku
  JWT reuses the `kubectl-sandbox-client-credentials` issuer, so
  `kubectl_sandbox_fixed_groups` (`tf/gitops/agent-machine-access`) branches
  `request.user.username == "haku-k8s"` → `["haku"]`, else
  `["kubectl-sandbox-users"]`. Eventually replace the `else` default with an
  **explicit SA→group allowlist** (claude SAs → `kubectl-sandbox-users`,
  `haku-k8s` → `haku`, anything unrecognized → no groups) so a typo'd/renamed
  SA fails closed instead of silently inheriting the sandbox group. Held off
  for now so the existing claude-sandbox JWT path soaks unchanged first; the
  `expected_group: haku` check on the `haku-k8s` entry in the
  `authentik-jwt-rotation` `rotations.yaml` is the interim fail-closed net (a
  mis-mapped Haku token aborts rotation rather than minting a mis-scoped JWT).
