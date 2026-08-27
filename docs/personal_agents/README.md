# Personal Agents

Decision record for self-hosted personal-agent infrastructure. The driving case
is a **public coding agent**: runs in the cluster, reachable behind Authentik
and restricted to one user, whose job is opening pull requests against public
repositories as `agentydragon-agent`. Two further agent functions — a
personal-data agent with network isolation and full traces, and a git-backed
knowledge garden — are scoped in [requirements.md](requirements.md) but not
built.

`public-coder-agent` (<../../cluster/k8s/agents/public-coder-agent/>) is
deployed and meets the four hard requirements: stand up, open a PR end to end
unaided, remember instructions across sessions, and be confinable to
domain-level egress. Open work and the personal-data-agent design are tracked in
the programme's plan directory under `plans/`.

## Where things are

| Document                                   | What it holds                                                                                         |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| [verdicts.md](verdicts.md)                 | **Start here.** Everything evaluated and closed, with the reason it closed — read before re-proposing |
| [findings/](findings/README.md)            | The numbered findings F1–F20, grouped by subject, with the measurements behind them                   |
| [requirements.md](requirements.md)         | The stated wants (C/W/P/H/K), cross-cutting and per-agent-function                                    |
| [success_criteria.md](success_criteria.md) | S1–S5: the observable pass conditions, and the S4 waiver for the public coder                         |
| [credential_proxy.md](credential_proxy.md) | The credential-substitution design: what shipped and what was rejected                                |
| [knowledge_garden.md](knowledge_garden.md) | The K1–K5 knowledge-garden tool evaluation, kept for haku to maybe adopt as a user interface          |

Findings are numbered in discovery order and referenced by number from cluster
manifests, so `F7` means the same thing here as in a comment on a Deployment.
Two conventions carry most of the value: **refuted hypotheses stay**, together
with what refuted them, and **measurements over assertions** — where a finding
says something works or does not, it carries the command output that
established it.

## What is knowingly not there

- **The harness/shell split.** The agent's commands run in the same container
  as the gateway — the documented second choice (C8, S5), not a shortfall. What
  it costs: a prompt injection that reaches the shell reaches the harness, and
  the credential-holding boundary is the proxy rather than the process.
- **Deny-by-default egress, for this agent only.** Switched off by decision
  rather than by failure; the passing configuration is kept commented in
  `cnp-egress.yaml` and `iron.yaml` and must be re-enabled together. Rationale:
  [success_criteria.md](success_criteria.md) § S4 waiver. Still a hard
  requirement for anything holding personal data.

## Rough edges

- **The credential path auto-updates.** The proxy image holding the GitHub and
  BuildBuddy tokens is self-built and tracks a moving tag under Flux image
  automation — defensible since the build is ours, but it should be a decision
  rather than a side effect; the plan's TODO owns making it one.
- **Every config-driven restart costs one prompt** on whatever session was
  live (F14). Upstream behaviour, self-healing, recurring.
