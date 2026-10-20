# Kubernetes access and temporary grants

Use your mounted kubeconfig through the configured proxy. It authenticates as your Sandbox's own
ServiceAccount; discover the account with `kubectl auth whoami`.

## Check standing access

`kubectl auth can-i --list -n <namespace>` helps discover permissions but can be incomplete. Check
the exact operation before performing it:

```bash
kubectl auth can-i get pods --subresource=log -n <namespace>
kubectl auth can-i create pods --subresource=exec -n <namespace>
```

Include the resource name for a named-resource grant; omit `-n` for cluster-scoped resources.
A named ServiceAccount TokenRequest can have its own RBAC grant; check its exact scope when the
task calls for one. A preset or selectable grant catalog does not prove current access.

For user-authorized work, use standing access when it safely suffices. Otherwise check an
appropriate auto-approved Action before submitting an Action requiring manual approval.

## Kubernetes admin Actions

The `kubernetes_admin` group performs operations under linked operator authority. Its presence
in the catalog does not grant an alternate ServiceAccount or establish auto-approval. Discover the
chosen operation with `agentplane actions show kubernetes_admin/ACTION`. Examples
include `pods_log`, `pods_exec`, and `resources_get`.

Check `agentplane actions policy` against the exact operation and arguments. If manual approval is
needed, explain why your own RBAC and other available routes cannot perform it, and submit only
the missing operation/scope using the [Action lifecycle](action_lifecycle.md). For a database
migration check, prefer a read-only query in the named database Pod over an administrative
mutation. Do not read or print secrets as a diagnostic shortcut, impersonate the operator, or use
another account's credentials after a refusal.

## Request a temporary PolicyBinding

When authorized work needs an additional PolicySet, ask for the narrowest existing set and the
shortest useful duration. Ask in plain terms, then submit the grant once the operator agrees or
the task plainly requires it. Do not retry a denied grant under another key or name.

A PolicySet can contain Kubernetes, Action, or egress rules. A PolicyBinding attaches existing
sets to your ServiceAccount. Discover set names with `kubectl get policysets -n <namespace>` if
allowed; otherwise ask the operator instead of guessing or creating a set.

Apply the binding with `kubernetes_admin` `resources_create_or_update`, using its discovered
argument schema. The manifest is the complete desired state:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicyBinding
metadata:
  name: <your-sandbox-name>-<purpose>
  namespace: <namespace-containing-the-policysets>
spec:
  subject:
    namespace: <your-serviceaccount-namespace>
    name: <your-serviceaccount-name>
  policySets: [<existing-policyset>]
  expiresAt: "<UTC-RFC3339-instant>"
```

Name your own subject from `kubectl auth whoami`. Use a binding name distinct from your Sandbox's
standing binding: replacing that binding would replace its existing grants. State the requested
set and duration in the Action title; durations are usually under a few hours.

Approval is not a live grant. After execution succeeds, inspect the new binding with
`kubectl get policybinding <name> -n <namespace> -o yaml` or a `resources_get` Action. Wait for the
relevant `ActionsReady`, `EgressReady`, or `KubernetesReady` condition to be `True`, then confirm
effective access through `agentplane actions policy`, `agentplane egress rules`, or the exact
`kubectl auth can-i` check. Reconciliation can take a moment. The Policy Controller deletes the binding at `expiresAt`;
if the task finishes sooner, delete only that temporary binding with `resources_delete`.
