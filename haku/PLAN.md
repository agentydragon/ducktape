# Haku — Personal Background Agent

Named after Spirited Away's Haku: a dragon who quietly helps run the household (and
needs his name kept in writing — hence the repo).

**This doc is forward-looking.** It holds the vision, the durable doctrine, and the
**not-yet-built / open design questions**. Implemented architecture and deployment
detail has moved to where the code lives — the component READMEs and the manual — and
git history holds the original full design rationale:

- What Haku is and how it reasons, the item contract, the dashboard spec, the
  credential/perimeter model: `haku/base/instructions.md` (+ `haku/base/sources/`,
  `haku/base/recipes.md`).
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
iterating the base (instructions/sources/recipes) until the items are genuinely good,
and the not-yet-built items below.

## Durable doctrine (keep honoring this as the system grows)

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
  (unopened mail), Grocy (expiring / below-minimum stock), Manifold. (The Authentik
  OAuth facades are _auth_ only — they forward the full tool set — so they don't
  substitute for this.)
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
it** — today operator-clickable `actions[]` + free-text feedback, evolving toward Haku
writing arbitrary interactive UI (inputs, interlinked pages, TSX/JS) that records what
the operator expressed into `haku-state` for Haku to reduce on its next run. The console
stays the operator's interface _with_ Haku; Haku just gets to shape it.

The hard constraint: the console must eventually hold secrets **Haku may not have** (the
Claude Code web session bearer that launches the routine — see `TODO.md` → _Console_)
and run privileged actions Haku itself can't. So agent-authored UI and the
secret-holding trusted code must sit on opposite sides of a real boundary.

### Two boundaries, not one

Different threats, different mechanisms — keep them distinct:

- **Confidentiality / integrity (k8s layer).** Haku must not _read_ the console's secrets
  or logs, nor _change_ its trusted code/perimeter without a PR. The existing agent RBAC
  is already shaped for this: `cluster-diagnostics-reader` is **secret-free** (no
  `secrets`/`pods/log`/`configmaps` cluster-wide; logs only in infra namespaces), so a
  console in its **own namespace** is invisible to Haku for free. Today the console runs
  **in `haku-sandbox`** (`haku-sandbox-admin` = full CRUD), so Haku can already read its
  git secret and exec its pod — **no boundary exists yet.** Splitting it out is the
  prerequisite for holding any agent-forbidden secret.
- **Confused deputy (browser layer).** Agent-authored UI runs in the **operator's
  authenticated browser**, same-origin with the console API and holding the Authentik
  session. So agent JS needn't _read_ a secret to abuse it — it can _call the privileged
  endpoint_ with the operator's authority. Hiding the secret server-side does nothing
  here. This is the boundary that actually constrains "Haku writes arbitrary TSX," and
  it's enforced in the browser (origin isolation), not in k8s.

### Trust zones

- **`haku-console` (trusted, new namespace).** Ducktape code, Bazel→GHCR→Flux — so its
  code, its **capability allowlist**, and its secrets change only by a merged PR. Holds
  the launch bearer; exposes the API (below); owns and renders privileged controls.
  Because it's **reviewed/released ducktape code, not agent-driven, it does not sit
  behind the `haku-mitmproxy` fence** — that fence exists to contain _the agent_; the
  console is contained by code review + the PR gate, so it gets ordinary
  (least-privilege) egress to reach the Claude Code web API. No `haku` RoleBinding → Haku
  can't read its secrets/logs or patch it.
- **Agent-UI render/sandbox (untrusted by construction).** Serves and runs the UI Haku
  authored into `haku-state`. Holds no secrets; may stay in `haku-sandbox` (Haku owning
  it costs nothing). In the 1b target it's a **separate browser origin**.
- **Haku agent (`haku-sandbox`).** Authors UI + content into `haku-state`; reads back
  traces. No access to the trusted side.
- **`haku-state` git.** The shared medium: Haku writes content+UI, the console writes
  operator traces, Haku reduces them.

### The API split

Two tiers the console treats completely differently:

- **Trace API (low-priv).** Records operator-expressed intent into `haku-state`. Exposed
  to agent-authored UI. **Grants Haku nothing new** — it already writes `haku-state`
  directly — so it's cheap and broad; its only real property is _consent fidelity_ (did
  the operator genuinely express this?), a UX/trust concern, not a confidentiality one.
- **Capability API (high-priv).** Anything using a console-only secret or acting on the
  world (launch the routine). The teeth live here. Per capability: rendered/owned by the
  **trusted shell** (never agent UI), fired only on a **genuine operator gesture** against
  **trusted-rendered copy** (not agent free-text, so Haku can't phish consent),
  CSRF-gated, and **audited** in the trusted namespace (the audit log doubles as the
  "recent executions" view). The allowlist stays **tiny and PR-gated** — the standing
  question per verb is "if Haku tricked the operator into firing this once, is the worst
  case acceptable?" (Yes for _launch the routine_; a future _move money_ verb changes the
  whole design.)

### Phasing

- **Phase 1 — declarative (1a).** A richer **typed widget schema** (containers, inputs,
  selects, links, sub-pages) emitted in `haku-state` and interpreted by a **trusted
  renderer** — _no agent JS executes_ — beyond today's `actions[]`. Still to build: an
  in-console **executions panel** and a **one-in-flight guard**. Both want a
  routine-runs-listing API — **none is known to exist** for `claude_code` routines
  (only the `/fire` endpoint), so the interim "review past runs" affordance is the
  deep-link to the routine's `claude.ai/code` page (built from the routine id alongside
  the fire URL); each fire also returns its session deep-link. **When the listing API
  surfaces and we build the panel, adopt the `anthropic` Python SDK** for the Anthropic
  calls (it auto-sends the `anthropic-version` header — the omission that 502'd the
  bare-`httpx` fire call — plus `auth_token` bearer, typed errors, and retries) and
  migrate the `launch-routine` POST onto it at the same time.
- **Phase 1b — free-form (the destination).** Haku runs **its own UI service** in
  `haku-sandbox` (its Deployment, its choice of runtime, app code from `haku-state`, no
  image build) and the console **embeds it in a cross-origin same-site iframe** — the
  console never renders or even sees Haku's UI. The trusted shell shrinks to: hold the
  bearer, own privileged actions, and frame the UI. Data + operator intent go straight to
  Haku's own backend (it has the `haku-state` creds); a minimal `postMessage` bridge
  carries only what needs trusted authority — **capability requests** (shell confirms +
  fires) and **`openLink`** (shell vets scheme + host whitelist, confirms off-whitelist,
  opens). Containment rests on
  cross-origin isolation (the bearer is unreachable from any browser context), Haku's UI
  being reachable **only** via the operator-owned **Authentik-gated** route (no public
  exposure), and the agent being unable to create public routes (its RBAC allowlist omits
  `httproutes`; harden the `from: All` gateway too). Full design + invariants + phasing:
  <console/plans/free_form_ui_iframe.md>.

### Server-side compute for agent UI

When agent-authored UI needs a backend, it talks to a **Haku-owned backend running in
`haku-sandbox`** (the agent's own perimeter), **separate from the console's** — one option
among several. This keeps the rule clean: code Haku authors never runs with the console's
secret privileges. The console backend stays the fixed, PR-gated capability/trace
provider; arbitrary agent compute lives agent-side, behind the agent's own fence.

### Open question — proving "this really came from the operator" to agent-side code

Phase 2 wants agent-side code (or the trace it reduces) to trust that an action
_genuinely_ came from the real operator, not from agent-authored UI forging it. Sketch
(operator, 2026-06-26): an **Authentik-native** attestation — e.g. give Haku its own
Authentik OIDC provider so the console can hand agent-side code a short-lived,
operator-authenticated token (or sign the trace with an operator-attested claim) that
agent-side code verifies. Open: exact shape (signed trace vs. a token the agent's backend
validates), how it composes with _Haku is its own principal_, and whether it's worth it
before free-form UI lands. Until then, consent fidelity rests on the trusted shell
rendering canonical copy for anything that matters.

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
