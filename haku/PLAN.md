# Haku — Personal Background Agent

Named after Spirited Away's Haku: a dragon who quietly helps run the household (and
needs his name kept in writing — hence the repo).

**This doc is forward-looking.** It holds the vision, the durable doctrine, and the
**not-yet-built / open design questions**. Implemented architecture and deployment
detail has moved to where the code lives — the component READMEs and the manual — and
git history holds the original full design rationale:

- What Haku is, its objective, and how it reasons, plus the credential/perimeter model:
  `haku/base/instructions.md` (+ `haku/base/sources/`). Base is **item-agnostic**; Haku's
  current working method — the item model + schema, its procedures, and its UI — lives in
  `haku/state_template/` (seeded into `haku-state`), not base.
- The run procedure: `haku/run.md`; the web runtime: `haku/runtime/claude_web_env/`.
- The dashboard service: `haku/console/README.md`. Alternative runtimes (Managed
  Agents): `haku/runtime/managed_agent/` + `haku/plans/`.
- Cluster wiring (RBAC, mitmproxy egress, secrets): `cluster/k8s/haku/` and
  `cluster/k8s/agents/haku-mitmproxy/`.
- The **actionable build checklist is `TODO.md`.**

## Goal

Haku runs in the background of the operator's life with a bundle of (mostly read-only)
access, continuously looking for useful things to do across everything it can see —
Gmail, Calendar, Drive, Tana, Plaid, the cluster, repos, and more as they're wired. It
acts autonomously where safe and read-only (scanning, cross-referencing, research,
synthesis), and produces **items** — concise, value-ranked recommendations — onto a
dashboard the operator reviews. Approving an item means **handing it off**: the item
carries a prepared prompt the operator takes as a URL into a scaffold (Claude app,
Claude Code) that does the work under its own permissions. Haku executing things itself
is a later direction (see _Future_), not the current contract.

Status: v0 is live — the **Claude Code web home** (`haku/runtime/claude_web_env/`) runs
the loop by hand/scheduled, driving the cluster over `kubectl` with `haku-sandbox` as
its compute surface. The data plane (scoped k8s identity + JWT rotation, the sandbox,
Plaid/Google read-only mirrors, `haku-state`, the console) is landed. Current work is
iterating the base (instructions/sources) and Haku's method (the procedures + UI in
`state_template`) until what Haku surfaces is genuinely good, and the not-yet-built items
below.

## Durable doctrine (keep honoring this as the system grows)

The doctrine's concrete realization — threat model, every enforcement mechanism with its
source-of-truth file, and the invariants edits must preserve — is indexed in
<base/SECURITY.md>; start a security review there.

- **The container is the trust boundary.** Assume anything reachable from Haku's
  container is fully available to the agent, and that prompt-injected instructions in
  any source (an email body, a transaction memo, a Tana note) can invoke anything Haku
  can. So the boundary is enforced **outside the agent** — at the credential / network /
  proxy perimeter — **never** by instructions or in-agent permission rules. (This is why
  the scanner runs `--dangerously-skip-permissions` behind scoped RBAC + a read-only
  credential set + mitmproxy egress, as a non-root pod.)
- **Read-only by construction**, in order of preference: (1) scope the upstream
  credential so it can't write (Plaid `plaid_ro`, the all-`.readonly` Google token);
  (2) filter the tool surface with a read-only MCP facade; (3) lock egress at L3/L4 +
  L7 via the dedicated `haku-mitmproxy` allowlist. Every credential reflected into
  `haku-sandbox` must be read-only/scoped.
- **Haku is its own principal** — it authenticates as itself everywhere (never the
  operator's session), for attribution, independent revocation, and a bounded blast
  radius.
- **base (read-only) vs. state (write).** Haku's instructions/schema live in
  `haku/base/` (ducktape, no write credential), so self-modification is a PR against
  ducktape, gated structurally. The only thing Haku writes is the `haku-state` repo.
  Operator base edits reach Haku by reconciliation (the `memory/base-sync.md` pin).
- **Everything is auditable**: every proposal/decision is a git commit; LLM traffic
  routes through LiteLLM (attribution/budget/kill-switch) with Langfuse traces.

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
transparency and containment that make the read-only posture safe.

Sketch to design out later (a real mechanism-design + security effort, not built):

- **Permission-elevation tokens.** The operator mints a scoped, expiring grant ("you
  may draft emails in account X", "you may browse the open web for N hours for
  research") that Haku may exercise only under defined, limited circumstances — a
  capability, not standing privilege; explicit, narrow, revocable. The default stays
  read-only.
- **Transparency by construction.** Every elevated action is logged and surfaced (what
  it did, under which grant, why), so the dashboard/log is the accountability surface —
  same as items are today.
- **Enforced by the perimeter, not by trust.** Per the doctrine above, an elevation
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

## The agent-authored console (free-form UI behind a trusted boundary)

Direction (operator, 2026-06-26): grow the console so **Haku authors more and more of
it** — from today's operator-clickable `actions[]` + feedback toward Haku writing
arbitrary interactive UI, with the trusted console shrinking to just the security
boundary. That boundary — the `haku-console` namespace split, and the low-privilege
**trace tier** vs. the secret-holding, CSRF-gated **capability tier** — is built; see
`console/README.md` for the current contract and
`console/plans/free_form_ui_iframe.md` for the boundary doctrine and the free-form design.

Still to build:

- **Richer declarative UI (1a).** A typed widget schema (containers, inputs, selects,
  links, sub-pages) emitted in `haku-state` and interpreted by a trusted renderer —
  _no agent JS_ — beyond today's `actions[]`. Plus an in-console **executions panel** + a
  **one-in-flight guard**; both want a routine-runs-**listing** API, and **none is known
  to exist** for `claude_code` routines (only `/fire`), so the interim "review past runs"
  affordance is the deep-link to the routine's `claude.ai/code` page. When a listing API
  surfaces and we build the panel, adopt the `anthropic` Python SDK (it auto-sends
  `anthropic-version` — the omission that 502'd the bare-`httpx` fire — plus bearer auth,
  typed errors, retries) and migrate the launch POST onto it then.
- **Free-form UI (1b).** Foundation shipped (2026-06-26): Haku runs its own UI service in
  `haku-sandbox` (reconciled from its `haku-state` `k8s/` under a constrained SA) and the
  console embeds it as a sandboxed cross-origin iframe — the **Free-form UI** tab — never
  rendering it itself. Still ahead: Haku replaces the placeholder with a real UI on its own
  backend; the shell gains a minimal `postMessage` bridge (`requestCapability` + a top-layer
  confirm, then `openLink`). **North star — base side landed:** the item model + schema +
  procedures have moved out of ducktape's base into agent-owned `haku-state` (seeded from
  `state_template/`), and base is now item-agnostic. Still ahead: the UI's styling/build and
  Flux image-automation become fully agent-owned, and the trace tier retires; the shell
  keeps only the boundary. Full design, containment invariants, and remaining order:
  <console/plans/free_form_ui_iframe.md>.

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
