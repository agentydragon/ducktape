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
  `haku-state` repo, not base (its former seed, `haku/state_template/`, was retired 2026-07-07).
- **Threat model, enforcement inventory, and the invariants edits must preserve:**
  <docs/security.md> — the durable doctrine's canonical home; start a security review there.
- The live primary runtime today is the manually configured Claude Code web home:
  `haku/runtime/claude_web_env/`, which runs `haku/run.md`.
- The trusted console (capability tier + iframe shell): `haku/console/README.md`;
  containment contract: `haku/console/docs/containment.md`. Alternative runtimes
  (Managed Agents): `haku/runtime/managed_agent/` + `haku/plans/`.
- Cluster wiring (RBAC, egress proxy, secrets): `cluster/k8s/haku/` and
  `cluster/k8s/agents/haku-egress-proxy/`.
- The **actionable build checklist is `TODO.md`.**

## Open directions

- **Make Haku substantially more useful.** Keep iterating the base method and the live
  haku-state procedures/UI until Haku routinely turns sources, memory, haku-ui, free
  tools, and approval-gated tool requests into high-value work the operator can approve
  with little effort. Durable doctrine belongs in `haku/base/instructions.md`; concrete
  method changes belong in the haku-state repo and the generic starter under
  `haku-state` (its former ducktape seed, `state_template/`, is retired).
- **More source coverage behind safe boundaries.** Add read-only facades or scoped
  credentials for sources that are not yet wired, such as PostScanMail and Manifold. Keep
  the durable boundary doctrine in `haku/docs/security.md` and per-source mechanics in
  `haku/base/sources/`; this plan should only name sources that remain to be added.
- **Console executions panel + one-in-flight guard.** Build this when there is a
  routine-runs listing API for Claude Code routines. The panel should render active/past
  routine state from an official listing and migrate launch calls to the `anthropic`
  Python SDK rather than extending bespoke `httpx` code.
- **Shared iframe bridge protocol — finish haku-state adoption.** The wire types + client
  helpers now live in the ducktape-owned `@haku/console-bridge` package
  (`haku/js/bridge_protocol/`, a package in the `ducktape_haku` shared-JS Bazel module), and
  the console shell imports them instead of defining its own copies. Remaining: haku-ui links
  the same package as a Bazel module (`bazel_dep(name = "ducktape_haku")` + `git_override`
  with `strip_prefix = "haku/js"` against the Forgejo ducktape mirror) and drops its
  hand-maintained `ui/frontend/src/bridge.ts` duplicate.
- **Alternative runtimes.** Keep the self-hosted/in-cluster scanner and Managed
  Agents worker as experiments at varying completeness. They are not the primary
  runtime unless their docs explicitly say they have replaced the Claude Code web
  home.
- **Tool-call expansion and richer Haku-owned workflows.** Connect more MCP/API servers
  and teach Haku's state/UI to use them well: prepared Tana edits, Gmail draft/send/archive
  flows, shopping/inventory check-ins, paperwork, and operations panels. Do not let any
  single button/widget surface become the whole product.
- **Capability registry.** Consider a small registry for service endpoints and credential
  sources if ad-hoc secret/config discovery starts costing Haku meaningful run time or causes
  drift.

## Future: more autonomous low-blast-radius tools

Consider letting Haku take **some** low-blast-radius actions autonomously — e.g. _draft an email_
(into Drafts, not send), _explore less-restricted websites_ for research, and similar moves —
without giving up the transparency and containment that make Haku's bounded posture safe. Existing
autonomous write exceptions belong in `haku/docs/security.md` and `haku/base/instructions.md`, not
here.

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

## A richer haku-console airlock (later)

haku-console may grow into an MCP/HTTP airlock proxy: calls that pass auto-allow policy execute
immediately, while all others become approval requests or async haku-ui continuations. The invariant
stays the same: exact call reviewed, trusted console approval, console-owned audit/result state,
credentials scoped or proxied rather than trusted to Haku's restraint.

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
