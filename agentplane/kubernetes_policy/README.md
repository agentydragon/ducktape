# Kubernetes Policy Controller

Keeps the native RoleBindings and ClusterRoleBindings that `PolicyBinding`s' Kubernetes rules expand
to. Contract: [`SPEC.md`](SPEC.md). The CRDs are in [`agentplane/crds`](../crds/BUILD.bazel) and
their parsed models in [`agentplane/policy/resources.py`](../policy/resources.py).

- [`expansion.py`](expansion.py): the pure expansion of the watched sets, bindings and accounts into
  native bindings and per-binding verdicts.
- [`controller.py`](controller.py): list-and-watch, the converging pass, and the `KubernetesReady`
  conditions.

## Running

The controller needs a YAML file named by `AGENTPLANE_KUBERNETES_POLICY_CONTROLLER_CONFIG_FILE`
([`settings.py`](settings.py)):

```yaml
policy_namespace: agentplane-staging
subject_namespaces: [agentplane-staging]
```

Its ServiceAccount needs to list and watch PolicySets, PolicyBindings and ServiceAccounts, apply
their status, read the referenced Roles and ClusterRoles, list RoleBindings and ClusterRoleBindings
cluster-wide, and create and delete them where sets grant, with `bind` on each referenced role.

```bash
bb run //agentplane/kubernetes_policy:main_bin -- --kubeconfig ~/.kube/config
```

## Design notes

- **No finalizer.** Each pass lists every native binding carrying this controller's label and
  deletes those no live, unexpired binding grants, so a `PolicyBinding` deleted while the
  controller was down loses its grants on the first pass after it starts. A finalizer would add
  nothing to that guarantee and would block deletion whenever the controller is down.
- **Native bindings are not watched.** Passes start on PolicySet, PolicyBinding and ServiceAccount
  changes, and our own writes would only trigger another pass. A native binding edited or deleted by
  hand is repaired on the periodic pass.
