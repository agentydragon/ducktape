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
  `agent-readable-namespace-metadata` baseline. Alongside core workload state, it covers
  safe operational metadata for autoscaling, disruption, standard/Gateway API networking, and
  Flux image automation. It deliberately excludes Secrets, ExternalSecret/SecretStore resources,
  Terraform controller objects, Helm/source/Kustomization values, Prometheus rules, pod logs,
  exec-like subresources, and all writes. The Namespace label is the required GitOps data
  classification for the non-secret controller configuration this makes readable. The existing
  `flux-system` Namespace is opted in by the patch at
  `cluster/k8s/flux/flux-system/kustomization.yaml`, so its image automation objects are
  covered without a cluster-wide grant.
- `rbac.ducktape.io/agent-readable-logs: "true"` binds that metadata baseline plus the additive
  `agent-readable-namespace-logs` role, which grants only `get` on `pods/log`.

Both classifications grant the same subjects: Haku's OIDC and synthetic access-profile groups,
its in-cluster ServiceAccounts, `kubectl-sandbox-users`, the synthetic public-coder group, and
agentplane-staging's `claude-ai` ServiceAccount. The Kyverno policy
`generate-agent-diagnostics-readers` (`cluster/cdk8s/kyverno/policies.py`) generates the
corresponding namespaced RoleBindings. Sensitive or identity-specific access remains explicit
service RBAC.

Augur is reconciled from `gaffer-private`, so its agent RBAC lives cross-repo at
`gaffer-private/k8s/parked/augur/agent-rbac/`. That directory also defines an
in-namespace Role granting `pods/exec`, `pods/attach`, and `pods/portforward` for
debugging the single-replica augur deployment.
