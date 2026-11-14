# Decoupling Agentplane from Kubernetes

Status: **no direction chosen, 2026-11-14.** The operator asked for this plan (UTC): Agentplane "might
be more decoupleable from kubernetes that way which could mean e.g. lighter integration tests. flux
could still configure with reconciliation via eg crossplane or such. with uncertainty of course, not
sure if that's a good idea" (01:10); "i'm pretty sure we could even make it possible to execute
commands in environments without k8s" (01:21). Decision node:
[`KUBERNETES_DECOUPLING_DESIGN`](task_dag.md#kubernetes_decoupling_design--how-far-to-decouple-from-kubernetes).

The [architecture overview](architecture_overview.md) prices where records live (D1–D4) and assumes
Sandbox workloads run on Kubernetes in every direction, as does the
[principles](../docs/principles.md) baseline ("Kubernetes is a shared baseline"). This plan adds two
motives that overview does not weigh, test weight and environments off Kubernetes, and the second one
would reopen that baseline. It does not repeat the overview's record-machinery analysis.

## What ties Agentplane to Kubernetes today

Checked on `devel` `a2c0b74e3`.

| Coupling                           | Where                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Caller authentication              | `kubernetes_bearer` TokenReview in History, Notifications, LLM ingress, the Action Service, the Runner Gateway and the app. Every SVID check makes one uncached GET of the `WorkloadCredential` and the `Identity` ([how a verifier resolves a peer](../docs/workload_authentication.md#how-a-verifier-resolves-a-peer)). cert-manager issues SVIDs from the `agentplane-workload` ClusterIssuer; the Policy Controller writes `WorkloadCredential` status.                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Agentplane records as CRDs         | CRDs in [`crds/manifests`](../crds/manifests): `Identity`, `PolicySet`, `PolicyBinding`, `WorkloadCredential`, `EgressCredential`, `SandboxTemplate`, `Sandbox`. The Action Service, egress and the MCP edge read them with their own informers ([`policy/reader`](../policy/reader)); only the app and browser use `PolicyService`'s watch RPCs. orchestration_v1 writes `Identity`, `PolicySet` and `PolicyBinding` through a kube client. CEL admission: `crds/test_*_admission.py`.                                                                                                                                                                                                                                                                                                                                                                                                          |
| Records whose output is Kubernetes | `KubernetesRoleSet`, `ServiceAccountGrant`, `ManagedServiceAccount` and `PolicySet` `kubernetes_*` rules become RoleBindings for agents that run `kubectl`. Product behavior; stays on Kubernetes under every option.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Environments                       | The Sandbox Controller creates the Pod or KubeVirt VM (Firecracker runs inside a Pod), state and workspace PVCs, launch ConfigMap and ServiceAccount. Its leader directly observes each accepted Runtime with `WatchState(observe_only=true)`; lifecycle facts stay in memory, fenced by Sandbox UID, accepted workload Pod UID and boot ID. Gateway replicas dial the Runtime directly at its Pod IP ([runner control](../docs/runner_control.md)); proxy callers use the stable `runner-gateway` Service. Runtime calls carry Sandbox UID, workload Pod UID and boot ID. Per-Sandbox control-Service and EndpointSlice creation and Gateway connection-Lease publication are removed; old route objects may remain inert through the operational rollout holds. `WorkloadCredential` `spec.pod` and Pod-bound tokens tie identity to the Pod. Egress is a sidecar or relay plus Cilium policy. |
| Placement and ownership            | The Dispatcher's ownership Lease ([`ownership.go`](../runner_gateway/dispatcher/ownership.go)); History, Notifications and the sampler already use [`postgres/lease`](../postgres/lease).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| API addresses                      | `EnvironmentTarget` is namespace, name and UID; `SubmitInput` and `InterruptTurn` carry `expected_sandbox_uid`; `Dispatch` names a Sandbox; Runtime proxy callers use the stable `runner-gateway` Service and send the `x-agentplane-sandbox*` identity headers.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

## What tests need today

- **k3s system suites** ([system tests](../system_tests/README.md)) each start a stack. A Pod stack
  became ready 50–150 s after k3s started and a VM stack about 200 s, mostly image import
  ([stack timings](../system_tests/k3s/debug/stack_timings.md)).
- **Central-sampler suites** (`test_sampler_roll`, `test_gateway_crash`, `test_history_outage`, the
  central half of `test_services`) sample in the central sampler, not in a Sandbox. They use a Pod
  only for the executor, because `central_sampler_v1` binds every central Session to a
  Sandbox-backed executor, and for the services' own Deployments. The
  central creation contract in review ([#15436](https://github.com/agentydragon/ducktape/pull/15436))
  proposes zero-environment central Sessions, which need no executor at all.
- **Commands already run in an environment that is not a Pod in tests.**
  [`central_sampler/test_gateway_integration.py`](../central_sampler/test_gateway_integration.py) and
  [`env/e2e`](../env/e2e/README.md) run the Runtime as a process or container and the Gateway as a
  process, with a bare kube-apiserver and no kubelet; [`sandboxes/testing/kubernetes.py`](../sandboxes/testing/kubernetes.py)
  writes the Pod record with a loopback IP. Kubernetes there is bookkeeping only.
- **About 47 Go test files** start a real API server (`testing/envtest`, `testing/kubeapiserver`)
  across the Action Service, egress, History, Notifications, the Dispatcher, LLM ingress, the
  Policy Controller, identity and the Sandbox Controller. They are not classified by reason; many
  need it only for TokenReview and policy reads.

## Options

The options compose; none excludes another.

### O1. API-server-only test tier

Run the services as processes against a bare kube-apiserver, with a Runtime process in place of the
Pod, as the two tests above do. No production change. It could carry the central-sampler suites and
Runtime-only Session flows. It cannot cover Pod or VM lifecycle, Cilium, egress sidecars or
Deployment rolls driven by the kubelet: a sampler or Gateway roll becomes process replacement, which
tests the services' drain and recovery but not readiness or endpoint switching.

### O2. Seam first

Production changes that remove incidental coupling without moving a store; each also stands on its
own:

1. Every policy, identity and credential read goes through `PolicyService`; the per-service informers
   go.
2. Callers authenticate with SVID mTLS everywhere and `kubernetes_bearer` goes (option 2 of
   [`PRIVILEGED_BACKEND_AUTH_DESIGN`](task_dag.md#privileged_backend_auth_design--one-service-authentication-mode)),
   with credential resolution through the same reader.
3. Dispatcher, EnvironmentService and CLI addresses become opaque environment IDs.
4. Gateway code supports independent zero-epoch Runtime watches and direct calls from every replica,
   fenced by the Controller-bound Sandbox UID, workload Pod UID and Runtime boot ID. Each replica
   dials the Runtime at its Pod IP; proxy callers use the stable `runner-gateway` Service. The
   per-Sandbox control-Service and EndpointSlice creation and connection-Lease publication paths
   have been removed, though old route objects may remain inert while operational rollout holds are
   open. See the [runtime state observation plan](runtime_state_observation.md) and [task DAG](task_dag.md#runner_gateway_cutover_rollout--operational-holds-for-the-prepared-cutover)
   for those holds. The Runtime receiver still tolerates legacy nonzero epochs until a later
   pin-gated cleanup. The Dispatcher delivery Lease is a separate scheduling protocol and remains
   unchanged.

Service tests could then run against one fake `PolicyService` instead of envtest. The store stays
an API server.

### O3. Environments without Kubernetes

An environment kind whose Runtime is a plain process or container outside the cluster: a test
process, a developer machine, a VM elsewhere. What it needs:

- **Registration.** An environment record the Sandbox Controller does not create, so it needs O2's
  opaque IDs.
- **Address.** The Gateway dials the Runtime at an address it derives from the Pod record's IP and
  the runner port. Without a Pod, the environment record carries the Runtime's address and the
  Gateway keeps dialing it. A Runtime-dials-Gateway mode used to be the default and was removed
  deliberately (operator, 2026-11-14); it does not come back for this.
- **Identity.** An SVID without a mounted Secret. The Runtime already accepts a SPIFFE Workload API
  socket (Firecracker uses one); something must attest the host instead of a Pod, such as a join
  token exchanged for a certificate.
- **Fencing.** The incarnation becomes the Runtime's boot ID instead of a Pod UID.
- **Lifecycle.** No Sandbox Controller suspend or resume: wake-on-send and idle suspend are
  unsupported or delegated to the host.
- **Egress.** No sidecar or Cilium. Egress policy and credential substitution do not apply unless
  the host routes through the egress proxy, and nothing enforces that. This is a security decision,
  not an implementation detail.
- **State.** The Runtime's `state_dir` lives on the host instead of PVCs.

Tests it would lighten: the central-sampler suites, Runtime and harness Session flows, parent
delegation through the CLI. Pod, VM and egress suites stay on k3s.

### O4. Move the store

[D4](architecture_overview.md#four-directions): `PolicyService` owns `Identity`, `PolicySet`,
`PolicyBinding` and `WorkloadCredential` as rows, and Flux writes through a Crossplane provider or a
git importer. Lost: CEL admission (becomes service validation), owner-reference cleanup (becomes an
explicit cascade), kubectl and RBAC as the write guard (becomes service authorization and audit),
and the apiserver watch (becomes a resumable watch). The overview prices it as expensive to build
and to back out. After O1–O3 it removes only the bare API server from the non-Sandbox tier.

## Recommendation (proposal, not a decision)

O2 first: each step is justified without decoupling. O1 as test work on the existing fixtures,
starting with the central-sampler suites. O3 as a design after O2, if environments off Kubernetes
are wanted as a product capability. O4 only if the API server left after O1–O3 still costs measured
test time; its startup cost was not measured here.

## Question for review

Which payoff is wanted: lighter tests, which O1 and O2 reach, or environments off Kubernetes as a
product capability (O3), which changes the principles' Kubernetes baseline?
