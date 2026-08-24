# Public coder tooling and approval playbook

This is the repository-owned operating guide for `public-coder-agent`. It explains which identity and
tool surface to prefer, how to check the current authority, and how to avoid unnecessary Haku
Console approval stalls without borrowing the Operator's authority.

The live credential, RBAC, and Haku Console configuration remain authoritative. This guide describes
how to inspect and use them; it does not grant access by itself.

## Golden rule

Use the narrowest identity that already has the required authority. Do not submit an approval-gated
Haku call merely because a generated Haku tool exists for the operation.

Operations on the public-coder Pod's own state are local operations. Files, checkouts, worktrees,
Git metadata, processes, caches, command output, installed tools, and tests inside the Pod must use
OpenClaw's local file, shell, and process tools. Do not send these operations through any Haku
Console MCP server, including `hostexec` or the sandbox server. Haku is an escalation and external
service boundary, not an alternate shell for the Pod in which the Agent is already running.

Use this order:

1. local workspace files, processes, tests, and desired-state Git;
2. direct Git or GitHub REST as `agentydragon-agent` through iron-proxy;
3. direct reader kubectl after checking its live RBAC when necessary;
4. an existing Haku auto-approved tool when it provides authority unavailable above; and
5. the narrowest approval-gated Haku operation that supplies the genuinely missing authority.

Keep unrelated work moving while an approval is pending. Withdraw a pending request as soon as a
direct route or other evidence makes it unnecessary.

## Check the current capabilities

Do not rely indefinitely on a remembered tool list or policy summary.

### Local and repository tools

Inspect the checkout and environment directly:

- use `git`, file reads, `rg`, tests, pre-commit, Bazel, and BuildBuddy locally;
- use the task's dedicated Git worktree rather than a shared checkout;
- source `.openclaw/ducktape-env.sh` before Ducktape validation; and
- install the workspace-managed hooks in every new Ducktape worktree.

A missing local binary is not, by itself, a reason to use `hostexec/bash`. Check the image-provided
closure and workspace-local tools first. If the operation targets Pod-local state, keep it local
even when a Haku tool exposes a superficially similar file, shell, Git, or sandbox operation.

### GitHub identity

`GH_PAT` is a non-secret placeholder that iron-proxy replaces only in authentication headers sent to
scoped GitHub hosts. Use it exactly like a real token without printing it.

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

### Kubernetes RBAC

Use the mounted kubeconfig and direct `kubectl`. Check uncertain operations with, for example:

```sh
kubectl auth can-i get pods -n public-coder-agent
kubectl auth can-i get nodes
kubectl auth can-i get secrets -n public-coder-agent
```

The current repository sources are under `k8s-reader/`. The standing
`haku:access-profile:public-coder` synthetic group is intentionally read-only and excludes Secrets,
Pod exec, live writes, and unbound namespaces/resources. Console derives that group only from the
deploy-owned access profile; it is not a caller credential. Selected Node and cross-namespace
projections may be available. Trust `kubectl auth can-i` and the API server's decision over this
prose summary.

The Haku-backed temporary-grant workflow is the escalation path when standing SAR denies a needed
Kubernetes request. Use the `kubernetes` MCP server's `can_i` first, then submit one approval-gated
`create_grant` call containing every exact scope/rule item needed for the task. All items created by
that call share one start and expiry; do not split a coherent debugging session into per-operation
approval work. End a set with one `release_grants` call containing the durable grant IDs returned
by `create_grant`. Release is deliberately sequential rather than transactional: if one item fails,
earlier releases remain effective, so reconcile the result with `list_grants`. The Operator UI
groups rows by source ToolCall and can revoke every still-active row from one approval together.

The proxy's static execution ceiling is `cluster-admin`, but the standing SAR group remains the
same narrow read-only subject. The ceiling alone grants nothing: without standing SAR coverage or a
matching active Agent-owned grant, the proxy denies the request. Exact grants may therefore include
Secrets, RBAC, writes, and other cluster-admin capabilities when the Operator explicitly approves
them. Long-running `watch`/log-follow and upgrades other than pod `exec`/port-forward remain
rejected. Active exec and port-forward streams are reauthorized every five seconds; a release,
revocation, or authorization failure closes them within that interval plus the Console
authorization timeout (eight seconds with the deployed defaults). Do not invent a competing
cluster-access mechanism in response to an approval stall.

### Haku Console availability and policy

Use the Haku Console passive status/reflection tools to determine which MCP servers and tool schemas
are currently available. Availability is not the same as auto-approval.

The policy source of truth is:

- `cluster/k8s/haku/console/config.yaml` for access profiles and policy composition;
- `haku/console/auto_approval.py` for evaluator semantics; and
- `haku/console/test_auto_approval.py` for accepted and rejected argument shapes.

Search for the `public-coder` profile and its root policy before making claims about what is
currently auto-approved. Conditional tools remain approval-envelope tools in the reflected catalog;
the policy may execute a matching call automatically after seeing its arguments.

The Haku ledger is authoritative for an individual call's final status, policy evaluation, and
whether an Operator approved it. Retained OpenClaw wrapper logs are useful audit evidence but include
polls, retries, withdrawals, and truncated results.

### Haku Console from Bash

OpenClaw's generated tool wrappers are not the only client surface. Code running locally in the
public-coder Pod can connect to the same Haku Console streamable-HTTP MCP endpoint from Bash or
Python. This is useful when a task benefits from ordinary shell composition, for example:

- filtering the reflected tool catalog or a read result with `jq`;
- constructing structured arguments from repository data;
- feeding a result into a local analysis pipeline; or
- writing a small, task-specific script around several explicit MCP calls.

Use the configured Haku Console URL and `HAKU_CONSOLE_TOKEN` through `HTTPS_PROXY`; the local token is
a non-secret placeholder that iron-proxy replaces only in the Haku `Authorization` header. Never
print or inline the token. A shell or Python client must speak MCP JSON-RPC over streamable HTTP and
handle its SSE-framed responses.

This client route does not grant different authority or bypass review. Calls still run as
`public-coder-agent`, use its `public-coder` access profile, and receive the same auto-approval,
manual-approval, denial, and ledger handling as calls made through OpenClaw's generated wrappers.
The local-Pod boundary also remains unchanged: use Bash MCP access for external Haku services, not
as an indirect way to operate on the public-coder Pod's own files, processes, checkouts, or tests.

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

Do not route these writes through Haku's Operator OAuth GitHub connection. That uses the wrong
principal and adds an unnecessary approval round-trip.

Use Haku's repository-scoped GitHub reads when they provide authority the direct credential lacks.
The main current example is read access to `agentydragon/gaffer-private`.

### Ducktape and local source inspection

Use the local worktree for source search, Git history, diffs, generated-file checks, and validation.
The checkout in the public-coder Pod and a checkout on `wyrm2` or `rugged` are different working
trees on different machines. Do not describe a host checkout as "local" to public coder.

Use the Pod's workspace or direct GitHub when the requested information is repository content or
public remote state that can be reproduced there. Host access is justified when the question is
specifically about host-resident state, such as that host checkout's dirty files, worktree layout,
local-only branch/ref, configured remotes, direnv environment, or private source unavailable to the
Pod.

If a source tree genuinely exists only on an approved host, request a narrowly bounded hostexec read
and explain why the local checkout or GitHub cannot answer the question.

### Kubernetes diagnostics

Try direct kubectl first for reads covered by the public-coder RBAC. Typical examples include the
agent namespace, Node inventory, Pod logs where granted, Ducktape Flux status projections, and VM
image publisher metadata.

For Flux-managed systems, change desired state in Git and verify reconciliation. Do not patch live
objects as a substitute for the Git change.

Use Haku/grants only when the required namespace, resource, subresource, or verb is absent from the
standing RBAC and the missing fact or action is necessary. Secrets, Pod exec, cluster writes, and
privileged node operations are not routine direct-reader diagnostics.

### Host execution

`hostexec/bash` is the high-permission escape hatch to physical operator machines such as `wyrm2`
and `rugged`. It runs under an explicitly authorized host user, commonly `agentydragon`, whose host
and cluster permissions can be much broader than the public-coder Pod's identities.

Central valid uses include:

- admin-level kubectl diagnostics or operations that the public-coder ServiceAccount and current
  temporary grants do not permit;
- reading host-local logs, such as a bounded excerpt from `/var/log/...`, when a bug exists on that
  physical machine;
- inspecting host-local services, devices, networking, files, checkouts, or environment state; and
- performing an explicitly requested host-side administrative operation under the approved user.

Before submitting it, check only that the call genuinely needs that host or wider authority:

1. Is the requested information reproducible from the checkout in the public-coder Pod or from the
   public GitHub remote, rather than being a fact about this host's checkout?
2. Is it only making a public or directly authenticated GitHub request?
3. Is it kubectl that the mounted reader RBAC permits?
4. Is the required result already available through a typed Haku tool with the necessary authority?

If any answer is yes, use that route instead.

Otherwise hostexec is the intended route. Preserve the full command exactly for approval, identify
the physical host and `run_as` user, cap output and runtime, avoid unnecessary secret values, and
state which host-local fact or elevated permission makes the direct Pod surfaces insufficient.

## Current auto-approval summary

As of 2026-08-21, `public-coder-agent` uses the `public-coder` access profile. Its standing Haku
policy auto-approves reviewed GitHub reads scoped to:

- `agentydragon/ducktape`; and
- `agentydragon/gaffer-private`.

The repository evaluator checks ordinary owner/repository fields and applies stricter parsing to
code and pull-request search queries. Other Haku operations remain approval-gated unless the live
configuration has changed.

Do not infer public-coder authority from the broader `haku_v1` profile. That profile belongs to Haku
and includes personal-service and other standing permissions that public coder must not inherit.

## Approval lifecycle

Treat approval as asynchronous work that can be front-loaded. Once a genuinely blocked operation
has exact, reviewable arguments, submit it early instead of postponing it until all unrelated work
is finished. Several independent calls may be pending at once; they do not need to be approved or
submitted serially.

For each approval-gated dependency:

1. finish enough local analysis to know the exact narrow operation and arguments;
2. submit the call with a specific title and rationale, preserving the exact command where one is
   involved;
3. record the returned `tool_call_id` and which work item depends on it;
4. submit other independent approval-gated calls too when their arguments are already known;
5. continue all local or otherwise unblocked work instead of waiting idly;
6. later read each existing call with `get_tool_call` rather than submitting a duplicate;
7. consume successful results, handle denial/error explicitly, and leave a still-needed slow
   request pending; and
8. withdraw the call immediately if it is superseded, the plan changes, or another route answers
   the question.

Do not front-load speculative calls whose arguments depend on an unfinished investigation or an
earlier result. Do not combine unrelated privileged operations into one broad Bash command merely
to reduce the number of approvals: multiple narrow, independently reviewable calls are preferable.
Likewise, a synchronous wait ending with `pending_approval` is not a failure and is not a reason to
resubmit the operation.

An approval authorizes only the reviewed call. Do not treat one approval as permission for a later
command, broader scope, or changed arguments.
