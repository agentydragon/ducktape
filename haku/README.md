# Haku — Personal Background Agent

Named after Spirited Away's Haku: a dragon who quietly helps run the household (and
needs his name kept in writing — hence the repo).

What it promises today: `SPEC.md`. Vision, not-yet-built work, and open design
questions: `PLAN.md`.

**Status:** v0 is live end-to-end — the Claude Code web home runs the loop scheduled, the
data plane (scoped k8s identity, the sandbox, read-only source mirrors, `haku-state`, the
mailbox — `haku@allegedly.works` on a self-hosted Stalwart, delivery DMARC-gated to
whitelisted senders in operator-owned config, read over JMAP with a dedicated Authentik
identity; `cluster/k8s/haku/mailbox/`) is landed, and Haku owns its UI service and method.
Current work is iterating that method until what it surfaces is genuinely good.

## Where things live

- What Haku is, its objective, how it reasons, and its working method: **not in ducktape.** They
  live in the `haku-state` repo's root cards (`AGENTS.md`, `SOUL.md`, `MEMORY.md`) and the hubs
  they point at, which Haku owns and writes. ducktape holds the runtime entrypoints and the deploy
  config — <base/README.md> says what is left here and why it stays.
- **Threat model, enforcement inventory, and the invariants edits must preserve:**
  <docs/security.md> — the durable doctrine's canonical home; start a security review there.
- The web runtime and its run procedure: `runtime/claude_web_env/` (+ its `run.md`).
- The trusted console (capability tier + iframe shell): `console/README.md`;
  containment contract: `console/docs/containment.md`. Alternative runtimes
  (Managed Agents): `runtime/managed_agent/` + `plans/`.
- The current colocated HTTP egress and bridge-bearer contract: `egress/SPEC.md`;
  research, alternatives, and decision history: `../cluster/docs/plans/agent_egress_proxy_options.md`.
- Cluster wiring (RBAC, egress proxy, secrets): `cluster/k8s/haku/` and
  `cluster/k8s/agents/haku-egress-proxy/`.
- The **actionable build checklist is `TODO.md`.**
