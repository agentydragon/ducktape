# agent-lab — time-boxed experiment namespace

Scratch namespace for mapping which **sandbox mechanism × agent harness**
combinations satisfy <../../../../plans/personal_agents/success_criteria.md>
(S1–S5). In scope: OpenShell, `agents.x-k8s.io` agent-sandbox, kagent
`AgentHarness`/`SandboxAgent`, OpenClaw, Hermes, and anything else worth trying.

**This grant is deliberately broad and deliberately short-lived.** It exists so
the experiments can run without a human approving each `kubectl exec`; it is not
a standing privilege.

## Removal

CLEANUP(added 2026-07-29): remove after **2026-07-30** by deleting this
directory, <../openshell/sandboxes-agent-rbac/>, both entries in the root
<../../kustomization.yaml>, and the `agent-lab` reflection targets in
<../../../../tf/gitops/litellm-keys/main.tf>. A revert PR follows the merge of
this one.

## What is granted

- **`agent-lab` namespace** — full CRUD on workloads, secrets, RBAC,
  NetworkPolicies, ExternalSecrets, and every installed sandbox/harness CRD.
- **`openshell-sandboxes` namespace** — read, `pods/exec`, and sandbox
  lifecycle (delete on `Sandbox`/`OpenShellSandbox`/pods), granted separately in
  <../openshell/sandboxes-agent-rbac/> so a missing or suspended namespace there
  cannot block this one. See the caveat below.
- **`openshellworkspaces`** (the one cluster-scoped CR in this set) —
  read-only.

Subject is the group `oidc-ksbx-groups:haku`. The pre-existing agent bindings
target `oidc-ksbx-groups:kubectl-sandbox-users`, which this session's identity
(`oidc-ksbx:haku-k8s`) is not a member of.

**`kubectl auth can-i` can lie here.** It has answered `yes` for permissions while
the real requests returned 403. Verify a permission with the actual call, never
with `can-i` alone.

## Model access

Experiments drive agents through the LiteLLM Codex-subscription lane. The
`litellm-key-openclaw` Secret is reflected into this namespace by the emberstack
reflector annotations in <../../../../tf/gitops/litellm-keys/main.tf>; no new
LiteLLM virtual key is minted for the lab, and the reflection target is removed
with the rest of the grant.

## Recording results

Findings — including failures, which are the point — go to
<../../../../plans/personal_agents/survey/README.md>, with the S-criterion they bear
on named explicitly.
