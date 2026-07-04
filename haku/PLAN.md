# Haku — Personal Background Agent

Named after Spirited Away's Haku: a dragon who quietly helps run the household (and
needs his name kept in writing — hence the repo).

**This doc is forward-looking.** It holds the vision and the **not-yet-built / open
design questions** — nothing else. Implemented architecture, deployment detail, and the
security doctrine live where the code lives; git history holds the original full design
rationale:

- What Haku is, its objective, and how it reasons, plus the credential/perimeter model:
  `haku/base/instructions.md` (+ `haku/base/sources/`). Base is **item-agnostic**; Haku's
  current working method — its presentation format, procedures, and UI — lives in its
  `haku-state` repo (seeded from `haku/state_template/`), not base.
- **Threat model, enforcement inventory, and the invariants edits must preserve:**
  <docs/security.md> — the durable doctrine's canonical home; start a security review there.
- The run procedure: `haku/run.md`; the web runtime: `haku/runtime/claude_web_env/`.
- The trusted console (capability tier + iframe shell): `haku/console/README.md`;
  containment contract: `haku/console/docs/containment.md`. Alternative runtimes
  (Managed Agents): `haku/runtime/managed_agent/` + `haku/plans/`.
- Cluster wiring (RBAC, egress proxy, secrets): `cluster/k8s/haku/` and
  `cluster/k8s/agents/haku-egress-proxy/`.
- The **actionable build checklist is `TODO.md`.**

## Goal

Haku runs in the background of the operator's life with a bundle of (mostly read-only)
access, continuously looking for useful things to do across everything it can see —
Gmail, Calendar, Drive, Tana, Plaid, the cluster, repos, and more as they're wired. It
acts autonomously where safe and read-only (scanning, cross-referencing, research,
synthesis) and surfaces concise, value-ranked recommendations in its own UI; approving
one means **handing it off** (e.g. a prepared prompt taken into a Claude scaffold that
does the work under its own permissions). Haku executing things itself is a later
direction (see _Future_), not the current contract.

Status: v0 is live end-to-end — the Claude Code web home runs the loop scheduled, the
data plane (scoped k8s identity, the sandbox, read-only source mirrors, `haku-state`,
the mailbox — `haku@allegedly.works` on a self-hosted Stalwart, delivery DMARC-gated to
whitelisted senders in operator-owned config, read over JMAP with a dedicated Authentik
identity; `cluster/k8s/haku/mailbox/`) is landed, and Haku owns its UI service and
method. Current work is iterating base and Haku's method until what it surfaces is
genuinely good, plus the items below.

## Not yet built

- **More sources behind read-only facades.** The read-only tool-filtering boundary
  already exists — the generic `mcp-oauth-facade` image (default-deny tool allowlist +
  a server-held upstream credential callers never see), live as `tana-mcp-ro`
  (`cluster/k8s/agents/tana-mcp-ro/`). An upstream MCP server with no
  read-only-credential trick gets fronted by **another instance** of it: a Deployment +
  a `config.yaml` allowlist + the upstream secret + a bearer-gated route — no new
  boundary code, the generalization is mechanical. Still to wire this way: PostScanMail
  (unopened mail), Manifold. (The Authentik OAuth facades are _auth_ only — they forward
  the full tool set — so they don't substitute for this.) **Grocy** went a different,
  cheaper route — no facade: its upstream enforces per-user permissions, so the read-only
  `haku` Grocy user (empty perms → API serves reads, 403s writes) _is_ the boundary, and
  Haku calls the grocy-sf MCP directly (`base/sources/grocy.md`). Prefer that whenever an
  upstream has its own read-only-credential model; the facade is for the ones that don't.
- **Console executions panel + one-in-flight guard.** Both want a routine-runs-**listing**
  API, and **none is known to exist** for `claude_code` routines (only `/fire`), so the
  interim "review past runs" affordance is the deep-link to the routine's `claude.ai/code`
  page. When a listing API surfaces and the panel is built, adopt the `anthropic` Python
  SDK (it auto-sends `anthropic-version` — the omission that 502'd the bare-`httpx`
  fire — plus bearer auth, typed errors, retries) and migrate the launch POST onto it then.
  (An earlier "richer declarative UI" direction — a typed widget schema rendered by the
  trusted console — is retired: the console renders nothing by design now; free-form UI in
  Haku's own iframe service superseded it.)
- **Share the iframe bridge protocol** instead of hand-duplicating the message shapes
  between `haku/console/frontend/bridge.ts` (authoritative) and Haku's UI — a tiny shared
  package or a sync-checked artifact. (The remaining cleanup from the realized free-form
  UI design; see `console/docs/containment.md` → _The bridge protocol_.)
- **In-cluster runtime** (the `haku-scanner` CronJob / self-hosted Managed-Agents
  worker) as an alternative to the web home — deferred; see `TODO.md` → _Later_ and
  `haku/runtime/managed_agent/`. Revisit if scanner-image upkeep or the
  client_credentials path proves painful (scheduled deployments + vaults remove exactly
  those work items).
- **Capability registry** (a ConfigMap mapping `service → facade URL → secret name`) —
  a possible later formalization of today's ad-hoc `kubectl get secret` discovery; not
  required by the current model.

## Future: letting Haku take some actions itself (permission-elevation tokens)

Today Haku is strictly read-only / synthesize-and-recommend: it never acts on the
world, it frames work for the operator to approve and hand off. A future direction
(operator, 2026-06-26) is to let Haku take **some** actions autonomously that aren't
allowed now — e.g. _draft an email_ (into Drafts, not send), _explore less-restricted
websites_ for research, and similar low-blast-radius moves — without giving up the
transparency and containment that make the read-only posture safe. (The `gmail-labeling`
closure server was the first realized instance of the pattern: a narrow write surface
made safe by construction — see `docs/security.md` inventory #7.)

Sketch to design out later (a real mechanism-design + security effort, not built):

- **Permission-elevation tokens.** The operator mints a scoped, expiring grant ("you
  may draft emails in account X", "you may browse the open web for N hours for
  research") that Haku may exercise only under defined, limited circumstances — a
  capability, not standing privilege; explicit, narrow, revocable. The default stays
  read-only.
- **Outbound mail to the operator** is the exception that may not need a grant at
  all: enabling submission on the mailserver (`cluster/k8s/haku/mailbox/`, currently
  receive-only) with a server-enforced recipient allowlist (`To: <operator>` only)
  has a blast radius of "can email the operator" — safe as standing capability.
  Needs deliverability work first: update the apex SPF `-all` / DMARC `reject`
  records in `tf/gitops/dns-records/`, OVH rDNS for the gateway IPs, and Gmail may
  still junk a fresh sender for a while.
- **Transparency by construction.** Every elevated action is logged and surfaced (what
  it did, under which grant, why), so the operator-facing surface is the accountability
  surface — same as recommendations are today.
- **Enforced by the perimeter, not by trust.** Per `docs/security.md`, an elevation
  must be enforced by what the token actually unlocks (the mechanism), never by trusting
  Haku to stay in bounds. Drafting (write to Drafts, no send) and sandboxed browsing
  are good first candidates — small, reviewable blast radius.
- **Open questions:** how grants are minted/stored (operator UI? a signed token in a
  secret?), how Haku proves it's acting under one, how scopes compose with the existing
  token/RBAC model, how "less-restricted browsing" stays contained, and where the line
  sits between "draft for review" and "act."

## A haku-owned execution tier (later maybe)

Beyond drafting, a fuller execution tier — verbatim tool calls replayed with elevated
creds on operator approval — remains a _later maybe_, not a third action kind. If built:
item-level approval of the exact tool calls is the gate, and `airlock/` (per-call HITL)
earns its place only if execution becomes agent-mediated (where what runs can diverge
from what was reviewed). Its credentials get the same treatment as everything else —
scoped or proxied, never trusted to the agent's restraint.

## Open questions

- **Value scoring**: single curator-owned 0–100 plus deadline is probably enough; resist
  building an expected-utility framework before the queue has real traffic.
- **Notification thresholds**: ntfy is the channel; when to ping vs. wait for a dashboard
  visit is a `memory/` matter, tuned via intake. Matrix is the richer later option if
  notifications ever grow replies.
- **Git as item store at scale**: a repo gives auditability, trivial backup, and
  human-editable state, but no queries or concurrent-writer safety. Fine at personal
  volumes with effectively serialized writers. If volume/concurrency ever outgrows it,
  add a read index (the repo stays source of truth) rather than moving authority to a DB.
- **Tighten the shared groups scope mapping** (deferred hardening): the Haku JWT reuses
  the `kubectl-sandbox-client-credentials` issuer; replace the `else`-defaults-to-sandbox
  group mapping with an explicit SA→group allowlist so a typo'd/renamed SA fails closed.
  Interim net: the `expected_group: haku` check in the `authentik-jwt-rotation`
  `rotations.yaml` aborts rotation rather than minting a mis-scoped JWT.
