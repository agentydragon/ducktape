This file intentionally avoids a hand-maintained namespace matrix. Service-specific and
sensitive grants are sourced from the set of `*rolebinding-*.yaml` files under:

- `cluster/k8s/**/agent-rbac/`
- `cluster/k8s/**/gateway-agent-rbac/`
- `cluster/k8s/agents/shared-rbac/`

Use `roleRef.name` in those files to determine which permission class is bound:
`namespace-diagnostics-reader`, `agent-readable-namespace-metadata`,
`agent-readable-namespace-logs`,
`logs-configmaps-reader`, or `secrets-reader`
(`secrets-reader` is an explicit per-service opt-in for
`kubectl-sandbox-users` only — never Haku), or a
service-specific reader Role/ClusterRole such as `ollama-reader`,
`langfuse-log-reader`, or `claude-props-reader`.

The common generated grants are exceptions to the file matrix. A GitOps-owned Namespace uses
one of two data-classification labels:

- `rbac.ducktape.io/agent-readable-metadata: "true"` binds the secret-free, log-free
  `agent-readable-namespace-metadata` baseline.
- `rbac.ducktape.io/agent-readable-logs: "true"` binds that metadata baseline plus the additive
  `agent-readable-namespace-logs` role, which grants only `get` on `pods/log`.

Both classifications grant the same subjects: Haku, its in-cluster ServiceAccounts,
`kubectl-sandbox-users`, and `public-coder-agent-reader`. The Kyverno policy at
`cluster/k8s/kyverno/policies/generate-agent-diagnostics-readers.yaml` generates the corresponding
namespaced RoleBindings. Sensitive or identity-specific access remains explicit service RBAC.

Augur is reconciled from `gaffer-private`, so its agent RBAC lives cross-repo at
`gaffer-private/k8s/augur/agent-rbac/`. That directory also defines an
in-namespace Role granting `pods/exec`, `pods/attach`, and `pods/portforward` for
debugging the single-replica augur deployment.
