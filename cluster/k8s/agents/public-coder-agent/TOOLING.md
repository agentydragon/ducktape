# Public coder tooling and access playbook

This is the repository-owned operating guide for `public-coder-agent`. It explains which identity and
tool surface to prefer and how to check the current Kubernetes authority without borrowing the
Operator's credentials.

The live credential, RBAC, and Haku Console configuration remain authoritative. This guide describes
how to inspect and use them; it does not grant access by itself.

## Golden rule

Use the narrowest identity that already has the required authority. Haku Console only authorizes
Kubernetes API requests for this Agent; it is not a general shell or tool gateway.

Operations on the public-coder Pod's own state are local operations. Files, checkouts, worktrees,
Git metadata, processes, caches, command output, installed tools, and tests inside the Pod must use
OpenClaw's local file, shell, and process tools. Do not send these operations through a Kubernetes
authorization proxy. It is not an alternate shell for the Pod in which the Agent is already running.

Use this order:

1. local workspace files, processes, tests, and desired-state Git;
2. direct Git or GitHub REST as `agentydragon-agent` through iron-proxy; and
3. direct reader kubectl after checking its live RBAC when necessary.

## Check the current capabilities

Do not rely indefinitely on a remembered RBAC or policy summary.

### Local and repository tools

Inspect the checkout and environment directly:

- use `git`, file reads, `rg`, and pre-commit locally;
- use the task's dedicated Git worktree rather than a shared checkout;
- source `.openclaw/ducktape-env.sh` before Ducktape validation; and
- install the workspace-managed hooks in every new Ducktape worktree.

A missing local binary is not, by itself, a reason to use a remote shell. Check the image-provided
closure and workspace-local tools first. If the operation targets Pod-local state, keep it local.

### GitHub identity

`GH_PAT` and `GITHUB_TOKEN` are the same non-secret placeholder. iron-proxy replaces either value
only in authentication headers sent to scoped GitHub hosts. Use them exactly like a real token
without printing them.

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

The actual bearer is reflected only into the trusted egress proxy, never into the OpenClaw
container. Requests to other hosts, methods, or paths retain the useless placeholder.

### ClickHouse analytics reader

`CLICKHOUSE_PUBLIC_CODER_USER=public_coder_analytics` and
`CLICKHOUSE_PUBLIC_CODER_PASSWORD` are the native ClickHouse reader identity for
normalized and raw AIQuota history. The password value in the OpenClaw runner is a
non-secret placeholder; Iron replaces it inside the HTTP Basic-auth header only
for the private ClusterIP host
`clickhouse.clickhouse.svc.cluster.local`. Do not print either value.

Use the existing HTTP proxy and ClickHouse's HTTP endpoint normally, for
example:

```sh
curl --fail-with-body --user "$CLICKHOUSE_PUBLIC_CODER_USER:$CLICKHOUSE_PUBLIC_CODER_PASSWORD" \
  --data-binary 'SELECT provider, max(observed_at), count() FROM aiquota.aiquota_windows GROUP BY provider FORMAT JSON' \
  http://clickhouse.clickhouse.svc.cluster.local:8123/
```

The native account has `SELECT` only on `aiquota.aiquota_windows` and
`aiquota.raw_http_observations`, under the bounded ClickHouse `readonly` profile
and quota. It has no `system.*`, DDL, or write access. `NO_PROXY` intentionally excludes the
ClickHouse Service, so bypassing Iron would send the useless placeholder and
fail; do not add `.svc.cluster.local` or the Service CIDR to it.

### Kubernetes RBAC

Use the mounted kubeconfig and direct `kubectl`. Check uncertain operations with, for example:

```sh
kubectl auth can-i get pods -n public-coder-agent
kubectl auth can-i get nodes
kubectl auth can-i get secrets -n public-coder-agent
```

The current repository sources are under `k8s-reader/`. The standing
`haku:access-profile:public-coder` synthetic group has secret-free read-only diagnostics plus
explicit per-service exceptions: the existing Agentplane staging lifecycle RoleBinding and GET of
only `public-coder-agent/agentplane-acceptance-operator`. The latter permits no Secret list/watch
or other Secret reads; acceptance bootstrap must capture the response without printing its values. Console derives that group only from the
deploy-owned access profile; it is not a caller credential. Selected Node and cross-namespace
projections may be available. Trust `kubectl auth can-i` and the API server's decision over this
prose summary.

When standing SAR denies a needed Kubernetes request, ask the Operator to issue a temporary grant
through Haku Console. Recheck with `kubectl auth can-i` before retrying the request, and ask the
Operator to release the grant when the work is complete.

The proxy's static execution ceiling is `cluster-admin`, but the standing SAR group remains the
same narrow read-only subject. The ceiling alone grants nothing: without standing SAR coverage or a
matching active Agent-owned grant, the proxy denies the request. Exact grants may therefore include
Secrets, RBAC, writes, and other cluster-admin capabilities when the Operator explicitly approves
them. Long-running `watch`/log-follow and upgrades other than pod `exec`/port-forward remain
rejected. Active exec and port-forward streams are reauthorized every five seconds; a release,
revocation, or authorization failure closes them within that interval plus the Console
authorization timeout (eight seconds with the deployed defaults). Do not invent a competing
cluster-access mechanism in response to an RBAC denial.

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

### Kubernetes diagnostics

Try direct kubectl first for reads covered by the public-coder RBAC. Typical examples include the
agent namespace, Node inventory, Pod logs where granted, Ducktape Flux status projections, and VM
image publisher metadata.

For Flux-managed systems, change desired state in Git and verify reconciliation. Do not patch live
objects as a substitute for the Git change.

When standing RBAC denies a necessary operation, ask the Operator to issue a temporary grant in
Haku Console. Secrets, Pod exec, cluster writes, and privileged node operations are not routine
direct-reader diagnostics.

### SSH to the devbox

`ssh devbox` reaches the dedicated devbox VM as the unprivileged `coder` account through a
terminating bastion (`cluster/k8s/agents/public-coder-agent/sshpiper`). It is a separate route from
the Haku Kubernetes authorization proxy.

Use it for watching a long build as it runs, interactive sessions, and `scp` (`rsync` is in neither
image).

Two things it deliberately cannot do, both rejected at the piper rather than by convention:
port forwarding (`ssh -L`/`-D`/`-R`), and reaching any account but `coder` — the destination user
is fixed upstream and no `ssh root@…` spelling changes it.

### Manual Agentplane acceptance from the devbox

The public-coder devbox is the controlled host for Agentplane's live acceptance targets. Run these
tests from the devbox, not from the OpenClaw Pod and not through `bbr`/RBE: the test process needs
the devbox's local Bazel execution, mediated kubeconfig, cluster route, and teardown lifecycle.

Use a fresh checkout path for each run and invoke only an explicit manual target:

```bash
ssh devbox
checkout=/home/coder/agentplane-acceptance-$(date -u +%Y%m%d%H%M%S)
git clone https://github.com/agentydragon/ducktape.git "$checkout"
cd "$checkout"
bazelisk test //agentplane/acceptance:test_mcp --test_output=streamed --test_arg=-s
```

The default acceptance target is `agentplane-testing` with `AGENTPLANE_ACCEPTANCE_IDP=dex` and its
dedicated operator Secret path. Staging remains available for ad hoc Authentik click-through tests
by overriding the URL, namespace, IDP, and Secret path. Do not put the acceptance
operator password or workload token in shell history, command arguments, checkout files, or test
artifacts. The devbox's mediated kubeconfig is the only supported way for the test to mint its
short-lived workload token and read the named operator Secret.

This is a manual behavioral test, not a CI gate. It creates real Sandboxes and can call the existing
cheap-model LiteLLM route; inspect the Action/egress evidence and complete teardown before starting
another run. Retain the devbox checkout only as long as needed for the run.
