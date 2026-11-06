# CI and BuildBuddy deployment options

Checked-in CI/BuildBuddy configuration and gates for evaluating other
topologies.

## Current configuration

- GitHub Actions runs the CI workflow. Bazel's workspace configuration sends
  build actions to BuildBuddy; `bbr` runs the outer Bazel invocation on a
  BuildBuddy Remote Runner. The ownership and locality contract is in the
  [Bazel configuration guide](../devinfra/docs/bazel_configuration.md),
  [BuildBuddy integration notes](../devinfra/buildbuddy/docs/integration.md)
  and [`bb remote` reference](../devinfra/buildbuddy/docs/remote.md).
- [`.bbrc`](../.bbrc) selects the hosted Remote Runner image and Firecracker,
  Docker-init and recycling properties. Its snapshot-save policy is deliberately
  unset. These settings describe the hosted path; they do not configure a local
  BuildBuddy server.
- The checked-in cluster configuration defines opt-in executor pools, not a
  self-hosted BuildBuddy control plane. The `wyrm2-kvm` OCI pool is configured
  with zero replicas and the `wyrm2-vm-tests` Firecracker driver is suspended.
  The separate `kvm-vm` worker is configured to connect to
  `remote.buildbuddy.io` for a manual, trusted-code test lane. Its setup,
  isolation boundary and recorded validation status live in the
  [KVM executor guide](../devinfra/buildbuddy/kvm/README.md) and
  [worker guide](../cluster/k8s/buildbuddy-kvm-worker/README.md).
- The worker image is named `buildbuddy-executor-enterprise`. That repository
  configuration does not establish current licensing eligibility, product
  support or pricing for other topologies. Confirm those directly with
  BuildBuddy before making a licensing or cost decision.

The [service reuse guide](../devinfra/buildbuddy/docs/service_reuse.md#local-execution-and-self-hosted-executors)
records additional executor lifecycle constraints. In particular, a self-hosted
executor and a self-hosted BuildBuddy service are separate deployment choices.

## GitLab evaluation boundary

The retained [2026-10-25 GitLab spike acceptance record](../cluster/debug/gitlab_spike/2026-10-25-live.md#ci-registry-and-flux-delivery)
documents Authentik OIDC login, a small Go test and `ko` image-publish path,
private registry access, Flux notification and deployment. That evidence does
not establish current live health, Dockerfile builds or general compatibility
with the repository's GitHub Actions workflows. Use the [spike configuration
and acceptance procedure](../cluster/x/gitlab_spike/README.md) for exact setup
and remaining replacement gates; do not copy a generic pipeline or Runner
example from this page.

## Decision gates

Keep the CI orchestrator, BuildBuddy service and executor placement as separate
decisions:

| Option                                                  | What it evaluates                                                                      |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| GitHub Actions + hosted BuildBuddy                      | Current baseline.                                                                      |
| GitLab spike + hosted BuildBuddy                        | CI migration without changing the RBE service.                                         |
| Existing self-hosted executor pools + hosted BuildBuddy | A constrained execution lane; not a self-hosted BuildBuddy service.                    |
| Self-hosted BuildBuddy service and executors            | A separate proposal requiring product, licensing, operational and workload validation. |

Before expanding a self-hosted topology or cutting over CI:

1. Confirm the desired boundary, current BuildBuddy edition and support terms,
   license entitlement and quote for the exact service/executor arrangement.
2. Validate the selected executor mode on its target nodes, including KVM and
   required isolation, image, storage and resource behavior. Use the checked-in
   executor configurations and their acceptance evidence; the old recipes and
   sizing tables in this page were not validated deployment guidance.
3. Run a representative Bazel workload through the candidate endpoint. Check
   remote execution and cache behavior, required Docker use, cancellation and
   cleanup, and the same assertions as the hosted path. The KVM guide links the
   current manual-lane evidence and its remaining limits.
4. For GitLab, prove each required workflow, private secret, artifact, registry
   publish, webhook and deployment path independently. In particular, choose
   and test the builder for workloads that need Dockerfile semantics.
5. Compare a vendor quote and measured infrastructure use with current hosted
   spend and ongoing operations. Keep the current workflows, credentials and
   hosted execution available until the required workloads and rollback path
   pass their acceptance gates.
