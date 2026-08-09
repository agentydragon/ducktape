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
  instead of standing up a second Deployment or a dedicated credential. Both Tana and
  Grocy went this way: Tana's standalone `tana-mcp-ro` facade was retired in favor of
  allowlisting `tana-rw`'s read tools in the console, and Grocy's dedicated read-only
  `haku` Authentik identity (`grocy-mcp-haku-sf`) was retired in favor of the console's
  existing `grocy-sf` entry — which also newly lets every runtime reach approval-gated
  Grocy _writes_, something the read-only credential structurally could never do; (2)
  **credential-scoping** — no facade at all, when the upstream itself enforces per-user
  permissions and Haku only ever needs read access with no path to writes-with-approval:
  cheaper than (1), but a dead end if write access is ever wanted, since the credential's
  identity has no write permission to begin with; (3) **a dedicated read-only facade** —
  the generic `mcp-oauth-facade` image (default-deny tool allowlist + a server-held
  upstream credential callers never see) for an upstream with neither of the above: a
  Deployment + a `config.yaml` allowlist + the upstream secret + a bearer-gated route —
  no new boundary code, the generalization is mechanical. Still to wire this way:
  Manifold. (The Authentik OAuth facades are _auth_ only — they forward the full tool
  set — so they don't substitute for this.)
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

## Future: broaden conversational Haku

The concrete milestone—one conversation-scoped sandbox waking from a durable `haku-state` checkout—is
tracked in [`plans/claude_sandbox_haku_runtime.md`](plans/claude_sandbox_haku_runtime.md).

After that path is reliable, possible extensions are named conversations and handoff/fork, scheduled
or event-driven wakes, and alternate message surfaces. All should reuse the same bounded runtime,
shared Haku identity, Console-owned policy/approval path, and Git concurrency rules rather than
introducing a second privileged execution path.

Agent-authored transcript content remains untrusted UI. Any inline executable action needs a
Console-owned schema, trusted rendering, actor checks, and the same audit path as an ordinary Console
action.

## Open questions

- **Value scoring**: single curator-owned 0–100 plus deadline is probably enough; resist
  building an expected-utility framework before the queue has real traffic.
- **Git as item store at scale**: a repo gives auditability, trivial backup, and
  human-editable state, but no queries or concurrent-writer safety. Fine at personal
  volumes with effectively serialized writers. If volume/concurrency ever outgrows it,
  add a read index (the repo stays source of truth) rather than moving authority to a DB.
- **Tighten the shared groups scope mapping** (deferred hardening): the Haku JWT reuses
  the `kubectl-sandbox-client-credentials` issuer; replace the `else`-defaults-to-sandbox
  group mapping with an explicit SA→group allowlist so a typo'd/renamed SA fails closed.
  Interim net: the `expected_group: haku` check in the `authentik-jwt-rotation`
  `rotations.yaml` aborts rotation rather than minting a mis-scoped JWT.
