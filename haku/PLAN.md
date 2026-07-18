# Haku — Personal Background Agent

**This doc is forward-looking.** It holds the vision and the **not-yet-built / open
design questions** — nothing else. What Haku is and its current status: `README.md`.
What it promises today: `SPEC.md`. Implemented architecture, deployment detail, and the
security doctrine live where the code lives (`README.md` → _Where things live_); git
history holds the original full design rationale. The **actionable build checklist is
`TODO.md`.**

## Not yet built

- **More sources behind read-only facades.** Three proven ways to make an upstream MCP
  server safe for Haku, cheapest first: (1) **console-side auto-approval** — when
  haku-console already reaches the upstream's full-tool server (`remote_server_oauth`/
  `static_bearer`), allowlist the specific safe tools in `console/auto_approval.py`
  instead of standing up a second Deployment (Tana went this way: the standalone
  `tana-mcp-ro` facade was retired in favor of allowlisting `tana-rw`'s read tools in
  the console); (2) **credential-scoping** — no facade at all, when the upstream itself
  enforces per-user permissions: **Grocy**'s read-only `haku` user (empty perms → API
  serves reads, 403s writes) _is_ the boundary, and Haku calls the grocy-sf MCP directly
  (`base/sources/grocy.md`); (3) **a dedicated read-only facade** — the generic
  `mcp-oauth-facade` image (default-deny tool allowlist + a server-held upstream
  credential callers never see) for an upstream with neither of the above: a Deployment +
  a `config.yaml` allowlist + the upstream secret + a bearer-gated route — no new
  boundary code, the generalization is mechanical. Still to wire this way: PostScanMail
  (unopened mail), Manifold. (The Authentik OAuth facades are _auth_ only — they forward
  the full tool set — so they don't substitute for this.)
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

Today's contract is read-only + hand-off (`SPEC.md`). A future direction
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
  it did, under which grant, why), so the operator-facing surface is also the
  accountability surface.
- **Enforced by the perimeter, not by trust.** Per `docs/security.md`, an elevation
  must be enforced by what the token actually unlocks (the mechanism), never by trusting
  Haku to stay in bounds. Drafting (write to Drafts, no send) and sandboxed browsing
  are good first candidates — small, reviewable blast radius.
- **Open questions:** how grants are minted/stored (operator UI? a signed token in a
  secret?), how Haku proves it's acting under one, how scopes compose with the existing
  token/RBAC model, how "less-restricted browsing" stays contained, and where the line
  sits between "draft for review" and "act."

## Haku Console risky-tool broker

Haku Console is the MCP/HTTP policy and approval boundary: calls that pass reviewed auto-approval
policy execute immediately, while all others become operator approval requests. The invariant stays
the same: exact call reviewed, trusted console approval, console-owned audit/result state, and
credentials scoped or proxied rather than trusted to Haku's restraint.

## Future: a conversational interface with Haku (operator, 2026-07-06)

Today the only way to reach Haku mid-stream is the console's capability tier: fire the
claude-code-web routine (optionally with per-run `text`) and get one fresh, fire-and-forget
`run.md` pass — no back-and-forth, no memory of the exchange beyond what lands in
`haku-state`. The operator wants something more like chatting with Haku directly, plus
richer push notifications. **Chat is the higher-priority half of this; notifications are a
nice-to-have.** Nothing below is designed yet — this is the shape of the ask, to work through
in a follow-up design pass.

- **A chat-like surface** — a Telegram bot and/or a web UI — where Haku reads messages and
  replies, as an easier and richer affordance than the console's launch dialog for:
  - **Quick dispatch**: send Haku a task in a message, superseding the console's
    "canned per-fire instructions" TODO (`TODO.md` → _Console_) with a lower-friction
    version of the same idea.
  - **An ongoing, longer conversation** — not just one-shot fire-and-forget: follow-ups,
    clarifying questions, iterating on a task, without re-stating context each time.
  - **Multiple conversation threads**, à la ChatGPT/claude.ai — separate topics or tasks
    kept apart rather than one running transcript.
  - **Inline action affordances** — clickable prebaked answers/actions in Haku's messages,
    not just prose. Conceptually the same idea as `haku-ui`'s markdown affordance widgets
    (`<signal-toggle>`, `<handoff>`, `<launch>`, `<feedback>`) but rendered in a chat surface
    (e.g. Telegram inline-keyboard buttons with `callback_data`, which is a different wire
    shape than a link-based affordance).
- **Richer mobile notifications** — push notifications (today: one-way ntfy, no replies, no
  buttons — see _Open questions_ below) that carry action buttons, so the operator can act
  from the lock screen instead of opening a dashboard.

Open design questions to work through before building anything:

- How a chat surface reconciles with Haku's run-based execution model: today each web-home
  invocation is one bounded `run.md` pass, not a standing process. Does "ongoing
  conversation" mean a live conversational loop (a new runtime shape), or per-message dispatch
  against a conversation thread/log kept in `haku-state` that each run picks up and appends to
  (closer to today's model, but not a live chat)?
- Where conversation threads live and who owns them — `haku-state` (consistent with "state is
  Haku's only memory") or a separate store, and how threads relate to `items/` and `runs/`.
- How action-button clicks get enforced: a chat message and click-to-action carries the same
  shape of risk as `requestLaunch`, but a bot API has no equivalent of "trusted-rendered
  chrome" to confirm against — worth a hard look before wiring any button to a mutating
  action.
- Bot/webhook identity and perimeter: a Telegram bot needs a public inbound route and its own
  credential, same class of new capability surface as `haku-ui`/the console — it inherits
  Haku's security doctrine (`docs/security.md`), not a chat SDK trusting the model to behave.
- Whether chat-dispatched one-off tasks run at Haku's own orchestrator privilege (like today's
  launch-routine) or can ever route through the dispatch plane's worker zones
  (`plans/multi_agent.md`).

## Open questions

- **Value scoring**: single curator-owned 0–100 plus deadline is probably enough; resist
  building an expected-utility framework before the queue has real traffic.
- **Notification thresholds**: ntfy is the channel; when to ping vs. wait for a dashboard
  visit is a `memory/` matter, tuned via intake. See _Future: a conversational interface
  with Haku_ above for the fuller chat/rich-notification direction this was gesturing at.
- **Git as item store at scale**: a repo gives auditability, trivial backup, and
  human-editable state, but no queries or concurrent-writer safety. Fine at personal
  volumes with effectively serialized writers. If volume/concurrency ever outgrows it,
  add a read index (the repo stays source of truth) rather than moving authority to a DB.
- **Tighten the shared groups scope mapping** (deferred hardening): the Haku JWT reuses
  the `kubectl-sandbox-client-credentials` issuer; replace the `else`-defaults-to-sandbox
  group mapping with an explicit SA→group allowlist so a typo'd/renamed SA fails closed.
  Interim net: the `expected_group: haku` check in the `authentik-jwt-rotation`
  `rotations.yaml` aborts rotation rather than minting a mis-scoped JWT.
