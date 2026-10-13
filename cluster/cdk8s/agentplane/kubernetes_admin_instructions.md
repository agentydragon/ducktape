### Kubernetes admin Actions for operations outside your own RBAC

Agentplane Actions has a `kubernetes_admin` group for Kubernetes operations performed under
linked operator authority. It is **not** an alternate ServiceAccount or permission granted by
access to the API. For a user-authorized probe or diagnosis, first check the exact verb,
resource/subresource, name and namespace with `kubectl auth can-i`. Use your own standing
kubectl access when it safely suffices. If it does not, discover the relevant operation with
`GET /v1/action-groups` on the Actions Service (described in the shared platform instructions),
and its arguments with
`GET /v1/action-groups/kubernetes_admin/actions/{action}`. Examples include `pods_exec`,
`pods_log`, and `resources_get`; choose the narrowest operation and arguments. For a database
migration check, a read-only `pods_exec` query in the named database Pod is preferable to a
broad administrative mutation. Do not read or print secrets as a diagnostic shortcut.

Inspect your `/v1/action-policy` bindings and `auto_approve_if` for the _exact_ action and
arguments before submitting. A Kubernetes admin Action may require manual operator approval;
never assume its presence in the catalog means it is auto-approved. If approval is needed,
explain why your own RBAC cannot perform the operation, submit a narrowly scoped request,
and wait for the decision and execution result via the Actions API/notifications as described
in the shared platform instructions. Never impersonate an operator, bypass the egress proxy,
or use admin Actions for unrelated work. Approval is not execution success.

### Asking the operator for a time-limited permission

When your task needs a grant you lack (a PolicySet's Kubernetes, Action or egress rules) and the
operator has authorized the task, ask for it instead of working around the refusal: do not reuse
another account's credentials, proxy around egress policy, or retry a denied call hoping it
changes. Ask in plain terms first ("May I have `<set>` for two hours to do X?"); the request
below is how the answer takes effect, so submit it once the operator agrees or the task plainly
requires it.

A permission is a `PolicyBinding` of existing PolicySets to your ServiceAccount, with an
`expiresAt`; the Policy Controller deletes it then, for every rule type. Apply it with
`kubernetes_admin` `resources_create_or_update` (the manifest is the complete desired state):

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicyBinding
metadata:
  name: <your-sandbox-name>-<what-for>
  namespace: <the namespace that holds the PolicySets; your own, from `kubectl auth whoami`>
spec:
  subject: { namespace: <your namespace>, name: <your ServiceAccount name> }
  policySets: [<existing PolicySet name>]
  expiresAt: "<UTC RFC 3339 instant>"
```

- Your ServiceAccount is the one `kubectl auth whoami` reports. Never name another subject.
- Request the narrowest PolicySet that covers the task and the shortest `expiresAt` that finishes
  it, usually under a few hours, and say both in the request title. Use a name distinct from your
  Sandbox's own binding: applying over that name would replace your standing grants.
- Only sets that already exist can be named. List them with `kubectl get policysets -n <namespace>`
  if allowed; otherwise ask the operator for the set name rather than guessing or defining a new
  set.
- Approval is not a live grant. After the Action succeeds, wait for the binding's `ActionsReady`,
  `EgressReady` or `KubernetesReady` condition to be `True` (`kubectl get policybinding <name> -o
yaml`, or `resources_get`), then confirm with `kubectl auth can-i`, `/v1/action-policy` or
  `/v1/rules`. A grant may take a moment to appear.
- When the task finishes before `expiresAt`, delete the binding with `resources_delete`.
- If the operator denies the request, report that and stop; do not retry under another key or name.
