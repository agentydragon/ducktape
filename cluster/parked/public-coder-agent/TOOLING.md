# Public coder tooling and access playbook

This is the repository-owned operating guide for `public-coder-agent`. It explains which identity and
tool surface to prefer. The live credential and egress configuration remain authoritative; this guide
does not grant access by itself.

## Golden rule

Use the narrowest identity that already has the required authority.

Operations on the public-coder Pod's own state are local operations. Files, checkouts, worktrees,
Git metadata, processes, caches, command output, installed tools, and tests inside the Pod must use
OpenClaw's local file, shell, and process tools.

Use this order:

1. local workspace files, processes, tests, and desired-state Git; and
2. direct Git or GitHub REST as `agentydragon-agent` through Agentplane egress.

## Check the current capabilities

Do not rely indefinitely on a remembered policy summary.

### Local and repository tools

Inspect the checkout and environment directly:

- use `git`, file reads, `rg`, and pre-commit locally;
- use the task's dedicated Git worktree rather than a shared checkout;
- source `.openclaw/ducktape-env.sh` before Ducktape validation; and
- install the workspace-managed hooks in every new Ducktape worktree.

A missing local binary is not, by itself, a reason to use a remote shell. Check the image-provided
closure and workspace-local tools first. If the operation targets Pod-local state, keep it local.

### GitHub identity

`GH_PAT` and `GITHUB_TOKEN` are the same non-secret placeholder. Agentplane egress replaces either
value only in authentication headers sent to scoped GitHub hosts. Use them exactly like a real
token without printing them.

The expected identity and permissions are:

- authenticated user: `agentydragon-agent`;
- upstream `agentydragon/ducktape`: read-only;
- fork `agentydragon-agent/ducktape`: admin/push; and
- `agentydragon/gaffer-private`: not visible through this credential.

When behavior matters, verify it through a narrow authenticated GitHub API read. Never print the
token or probe unrelated repositories.

### AIQuota read API

`AIQUOTA_API_BEARER_TOKEN` is a non-secret placeholder. Through the configured HTTPS proxy, it is
replaced with AIQuota's single shared bearer only for these exact read routes on
`https://aiquota.allegedly.works`:

- `GET /v1/quotas`; and
- `GET /v1/providers/{claude|codex}/raw`.

Use it as a normal bearer without printing it, for example:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $AIQUOTA_API_BEARER_TOKEN" \
  https://aiquota.allegedly.works/v1/quotas
```

The actual bearer is reflected only into the trusted Agentplane egress gateway, never into the OpenClaw
container. Requests to other hosts, methods, or paths retain the useless placeholder.

### ClickHouse analytics reader

`CLICKHOUSE_PUBLIC_CODER_USER=public_coder_analytics` and
`CLICKHOUSE_PUBLIC_CODER_PASSWORD` are the native ClickHouse reader identity for
normalized and raw AIQuota history. The password value in the OpenClaw runner is a
non-secret placeholder; Agentplane egress replaces it inside the HTTP Basic-auth header only
for the private ClusterIP host
`clickhouse.clickhouse.svc.cluster.local`. Do not print either value.

Use the existing Agentplane egress proxy and ClickHouse's HTTP endpoint normally, for
example:

```sh
curl --fail-with-body --user "$CLICKHOUSE_PUBLIC_CODER_USER:$CLICKHOUSE_PUBLIC_CODER_PASSWORD" \
  --data-binary 'SELECT provider, max(observed_at), count() FROM aiquota.aiquota_windows GROUP BY provider FORMAT JSON' \
  http://clickhouse.clickhouse.svc.cluster.local:8123/
```

The native account has `SELECT` only on `aiquota.aiquota_windows` and
`aiquota.raw_http_observations`, under the bounded ClickHouse `readonly` profile
and quota. It has no `system.*`, DDL, or write access. `NO_PROXY` intentionally excludes the
ClickHouse Service, so bypassing egress would send the useless placeholder and
fail; do not add `.svc.cluster.local` or the Service CIDR to it.

## Current preferred surfaces

### Public GitHub development

Use direct Git and GitHub REST as `agentydragon-agent` for ordinary contribution work:

- fetch public upstream branches;
- create branches and push commits to `agentydragon-agent/ducktape`;
- create or update pull requests from the fork to `agentydragon:devel`;
- add task-related issue or pull-request comments; and
- read public files, commits, checks, logs, issues, and pull requests.

Never push to upstream after a 403; it is expected. Never merge automatically, even if an API call
would technically succeed.

`agentydragon/gaffer-private` is not reachable from this Pod: this credential cannot see it.

### Ducktape and local source inspection

Use the local worktree for source search, Git history, diffs, generated-file checks, and validation.
The checkout in the public-coder Pod and a checkout on `wyrm2` or `rugged` are different working
trees on different machines. Do not describe a host checkout as "local" to public coder.

Use the Pod's workspace or direct GitHub when the requested information is repository content or
public remote state that can be reproduced there. Host access is justified when the question is
specifically about host-resident state, such as that host checkout's dirty files, worktree layout,
local-only branch/ref, configured remotes, direnv environment, or private source unavailable to the
Pod.

This Pod has no host-execution capability. If the answer depends on workstation-local state that the
Pod and public GitHub cannot show, ask the Operator to collect that evidence.

### Cluster changes

This Pod has no Kubernetes API access. Change desired state of Flux-managed systems in Git, and ask
the Operator for live cluster state.

The retired public-coder devbox was the controlled host for Agentplane's manual live acceptance
targets. No replacement host is implied by this guide; choose an explicitly authorized environment
and identity before running those targets.

## Egress rules

Run `agentplane egress rules` to inspect this workload's current access and credential
presentations. `agentplane egress guide` and `agentplane egress guide credentials` print the
workflow guides. The local gRPC route supplies this Pod's identity; keep proxy settings and CA
trust for subsequent outbound requests.
