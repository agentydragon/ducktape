# OpenHands TODO

> **Status: parked.** The manifests are parked under `cluster/parked/`. Revive the package before working on this backlog.

## Self-hosted git provider

OpenHands supports GitHub and GitLab natively (PAT or OAuth App). Consider pointing
it at a self-hosted option — either cluster Forgejo (`git.allegedly.works`) or a
self-hosted GitLab instance — so agent work can land in private repos without relying
on `github.com`. GitLab PAT just needs `GITLAB_TOKEN` env var (same pattern as
`GITHUB_TOKEN`); Forgejo would need a git-credential helper or embedded PAT in URLs
since there's no first-class Forgejo provider in OpenHands.

## Sandbox egress isolation

- [ ] Add NetworkPolicy/CiliumNetworkPolicy to `openhands-sandboxes` namespace blocking
      egress to internal cluster IP ranges (pod CIDR `10.244.0.0/16`, service CIDR
      `10.96.0.0/12`). Only allow egress to external internet + DNS (port 53 to
      kube-dns). Agent pods shouldn't reach internal cluster services by default.
