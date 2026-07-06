# Haku — Personal Background Agent

Named after Spirited Away's Haku: a dragon who quietly helps run the household (and
needs his name kept in writing — hence the repo).

Haku runs in the background of the operator's life with a bundle of (mostly read-only)
access, continuously looking for useful things to do across everything it can see —
Gmail, Calendar, Drive, Tana, Plaid, the cluster, repos, and more as they're wired. What
it promises today: `SPEC.md`. Vision, not-yet-built work, and open design questions:
`PLAN.md`.

**Status:** v0 is live end-to-end — the Claude Code web home runs the loop scheduled, the
data plane (scoped k8s identity, the sandbox, read-only source mirrors, `haku-state`, the
mailbox — `haku@allegedly.works` on a self-hosted Stalwart, delivery DMARC-gated to
whitelisted senders in operator-owned config, read over JMAP with a dedicated Authentik
identity; `cluster/k8s/haku/mailbox/`) is landed, and Haku owns its UI service and method.
Current work is iterating base and Haku's method until what it surfaces is genuinely good.

## Where things live

- What Haku is, its objective, and how it reasons, plus the credential/perimeter model:
  `base/instructions.md` (+ `base/sources/`). Base is **item-agnostic**; Haku's current
  working method — its presentation format, procedures, and UI — lives in its
  `haku-state` repo (seeded from `state_template/`), not base.
- **Threat model, enforcement inventory, and the invariants edits must preserve:**
  <docs/security.md> — the durable doctrine's canonical home; start a security review there.
- The run procedure: `run.md`; the web runtime: `runtime/claude_web_env/`.
- The trusted console (capability tier + iframe shell): `console/README.md`;
  containment contract: `console/docs/containment.md`. Alternative runtimes
  (Managed Agents): `runtime/managed_agent/` + `plans/`.
- Cluster wiring (RBAC, egress proxy, secrets): `cluster/k8s/haku/` and
  `cluster/k8s/agents/haku-egress-proxy/`.
- The **actionable build checklist is `TODO.md`.**
