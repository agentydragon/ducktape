# Shared test cluster

The [contract and rollout requirements](SPEC.md) describe the dedicated persistent K3s
node, infrastructure ownership and remaining test integration. The candidate Flux
deployment is suspended until its runtime and admission checks are complete.

## Trusted per-run setup

Run this outside the Bazel test action, with the inner cluster's admin kubeconfig:

```bash
bb run //agentplane/system_tests/shared_cluster:lease_bin -- \
  --admin-kubeconfig /path/to/inner-admin.kubeconfig allocate \
  --server https://api.agentplane-test-cluster.svc.cluster.local:6443 \
  --output /tmp/agents/YYYY_MM_DD/task/run
```

The new output directory is mode 0700; its files are mode 0600. Only
`application-token` and `observer.kubeconfig` go to the test driver. Keep
`allocation.json` with trusted setup for cleanup:

```bash
bb run //agentplane/system_tests/shared_cluster:lease_bin -- \
  --admin-kubeconfig /path/to/inner-admin.kubeconfig cleanup \
  --allocation /tmp/agents/YYYY_MM_DD/task/run/allocation.json
```

Cleanup waits for the namespace controller and retains capacity if deletion is stuck.
The default one-hour expiry also lets the trusted Kyverno cleanup controller reap an
abandoned run. The test application deployment and driver handoff are not wired yet.

## Observer permission checks

`check_permissions(api_client=..., namespace=...)` reviews the supplied client's authorization using
Kubernetes `SelfSubjectAccessReview`. It returns one `PermissionEvidence` per reviewed permission,
or raises `PermissionCheckError` with the collected evidence when a decision is unexpected or
incomplete. An incomplete decision has `allowed=None`. API transport errors propagate and fail the check.

Reviews observe current authorization; the POST does not write the reviewed resource. Workload
permissions are checked in the supplied namespace and the selected shared namespaces `default`,
`kube-system` and `kubevirt`. KubeVirt operator configuration is checked in `kubevirt`, and
cluster-scoped permissions are checked cluster-wide. Impersonation and CSR checks use selected
resource names. The caller supplies the client and identity explicitly.

The matrix is diagnostic, not a complete authorization proof. It does not enumerate every namespace
or resource name, and it does not test admission behavior or guarantee later decisions. Unit tests
fake only the API request boundary. A separate test uses the existing envtest API server with a real
ServiceAccount token and RBAC Roles to check one minimal observer policy and confirm that a wildcard
grant is rejected by the checker.
