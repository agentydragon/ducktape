# Personal Agents

Research record for self-hosted personal-agent infrastructure. The driving case is a
**public coding agent**: runs in the cluster, reachable behind Authentik and restricted
to one user, whose job is opening pull requests against public repositories as
`agentydragon-agent`. Two further agent functions — a personal-data agent with network
isolation and full traces, and a git-backed knowledge garden — are scoped in
[requirements.md](requirements.md) but not built.

Four hard requirements: stand up, open a PR end to end unaided, remember instructions
across sessions, and be confined to domain-level egress. A stronger sandbox around the
agent's hands than around the harness is a **want**, not a requirement.

## Status

`public-coder-agent` is deployed and meets all four. The evidence for each, and the
configurations that failed on the way, are in [lab_notes.md](lab_notes.md).

## Where things are

| Document                                                   | What it holds                                                                                       |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| [lab_notes.md](lab_notes.md)                               | **Start here.** The configurations tried and how each scored against S1–S5                          |
| [findings/](findings/README.md)                            | The numbered findings F1–F19, grouped by subject, with the measurements behind them                 |
| [requirements_status.md](requirements_status.md)           | Every stated requirement (C/P/W) against what is deployed, with evidence                            |
| [success_criteria.md](success_criteria.md)                 | S1–S5: the observable pass conditions, and what a failure of each would force us to decide          |
| [credential_proxy_options.md](credential_proxy_options.md) | Survey of credential-injecting proxies: three architectural camps, what was tested, where we landed |
| [TODO.md](TODO.md)                                         | Open work, each item carrying the evidence for why it matters                                       |
| [requirements.md](requirements.md)                         | The original stated wants, cross-cutting and per-agent-function                                     |
| [survey/](survey/README.md)                                | Requirement-to-implementation mapping with citations, from the phase that preceded the lab          |
| [manifests/](manifests/)                                   | Lab manifests reproducing the tested shapes                                                         |

Where the survey and the findings disagree, the findings win: one is what the
documentation claimed, the other is what the cluster did.

## Reading it

Findings are numbered in discovery order and referenced by number from cluster
manifests, so `F7` means the same thing here as in a comment on a Deployment. The index
at the top of the findings section groups them by subject for browsing.

Two conventions carry most of the value:

- **Refuted hypotheses stay**, together with what refuted them. Several plausible
  explanations turned out wrong, and the record is worth more for keeping them.
- **Measurements over assertions.** Where a finding says something works or does not,
  it carries the command output that established it.
