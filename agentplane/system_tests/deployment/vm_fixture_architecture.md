# VM system-test fixture options

The required VM suite runs on SSD-backed BuildBuddy workers. This is an open comparison of options
that require no new paid service or license. A
shared cluster could remove the inner control-plane startup and move image delivery out of the
executor, and changes who can act on which cluster. Use the measured SSD warm path as a baseline
and check the shared-cluster permission and storage contracts before choosing a replacement.

## Current execution and isolation

GitHub Actions starts a remote Bazel invocation. Bazel dispatches the VM test to a BuildBuddy
executor in a KubeVirt worker VM on the main cluster. The executor starts the test in Docker,
which runs the inner K3s API and kubelet. Inner KubeVirt boots two Agentplane guests with nested
KVM. The outer KubeVirt VM is the isolation boundary for an executor whose actions get the
guest's Linux capabilities and `/dev`. The inner K3s API gives each fixture administrative
control of its own CRDs, admission, certificates, PVs and workloads. The outer API token and
host filesystem are absent from the test container. Jobs are trusted with respect to the worker
guest, including its BuildBuddy credentials and disks. A Docker container on this pool provides
no hostile-job isolation from other actions on the same guest.
[CI routing](../../../devinfra/ci/README.md#where-bazel-ci-runs-tests) ·
[Worker runbook](../../../cluster/k8s/buildbuddy-kvm-worker/README.md) ·
[Executor configuration](../../../devinfra/buildbuddy/kvm/worker/executor.yaml)

The original worker decision preserves the existing K3s tests and adds hardware acceleration.
The first worker PR records that Docker's default AppArmor profile denied the mounts K3s needed.
The K3s follow-up used guest ext4 and made the action's private network sysctls and cgroup subtree
writable so the nested kubelet, kube-proxy and container runtime could start. Inner `virt-handler`
also needs shared mount propagation for its kubelet Pod mounts. These are worker-node requirements
from bundling the API and agent in the same K3s process. With a bare API server in a Pod, the
kubelet and KVM access belong on the worker. A non-root KVM device probe had already succeeded
before the full nested K3s action could start.
[Initial KVM issue](https://github.com/agentydragon/ducktape/issues/14034) ·
[Static worker PR](https://github.com/agentydragon/ducktape/pull/14072) ·
[Nested K3s PR](https://github.com/agentydragon/ducktape/pull/14094) ·
[Mount and cgroup setup](../k3s/baseline.py) · [KubeVirt install](../vm/infrastructure.py)

K3s lets the Bazel action bring its exact manifests and OCI layouts and own the whole fixture
lifecycle. KubeVirt is part of what the suite exercises. It drives real Sandbox and VMI
reconciliation, launcher admission for egress and virtiofs, workspace block and state filesystem
storage, guest systemd and Runner behavior, Claude and Codex turns and History, and graceful
ACPI shutdown through deletion. KVM accelerates the guests. The same cases can run with QEMU
emulation, and the paired trials led to the required route moving to hosted KVM.
[VM scenarios and fixture composition](README.md#vm-sandboxes) ·
[Required KVM cutover](https://github.com/agentydragon/ducktape/pull/18046) ·
[Paired trial evidence](https://agentydragon.github.io/ducktape/latency/#vm-shadow)

Three lifetimes affect the cost:

- **Worker VM and work disk.** Docker image layers, Bazel input file cache, and the worker-wide
  K3s image store can outlive an action or a worker restart on the same claim. Scale-in or
  replacement that deletes the claim loses its caches.
- **Docker runner and inner API.** Required `suites:test_vm` enables Docker runner recycling.
  On a compatible warm hit, K3s, its datastore and containerd, PKI, KubeVirt and Kyverno stay
  alive. The K3s data lives on the retained runner's private ext4 loop mount, outside the
  worker-wide mount another action can reset. BuildBuddy chooses whether it can reuse a runner.
  The fixture checks the clean marker, fingerprint, server identity and health. It rebuilds
  when compatibility or health fails and requests disposal on action failure. Manual
  `x/vm_kvm:test_vm_kvm` stops its server and creates a new API and controllers each
  action. It can keep the worker-wide image store after a clean stop. The store must match
  the K3s binary and have sufficient free space. Image reuse also checks the exact manifest,
  containerd content and unpack state, and CRI pin.
- **Test workloads.** Both paths recreate the application services and database, model and trace
  endpoints, fixture CA and credentials, Sandboxes and guest storage. The retained path must
  verify workload namespaces, fixture cluster objects and bindings, and workspace PVs and
  loop devices are gone before certifying the baseline clean. Fixture names such as `systest`
  and `monitoring` are reused inside the private API.
  [VM target definitions](../vm/defs.bzl) · [Required target](../suites/BUILD.bazel) ·
  [Manual target](../x/vm_kvm/BUILD.bazel) · [Baseline checks](../k3s/baseline.py) ·
  [Private disk and disposal](../k3s/recycling_probe.py) · [Fixture teardown](stack.py)

## Costs by fixture state

The 21 November disk trial ran the same uncached manual TestRunner action once per cell on
`wyrm2`, with identical worker image and sizing and a fresh 80 GiB work disk for each storage arm.
All four actions passed both cases. Times are seconds. TestRunner process time excludes worker
preparation, Bazel input staging, output upload and queueing. Fixture setup is inside that process
clock. The import envelope overlaps infrastructure work inside setup.
[Disk trial method and receipts](../../../devinfra/buildbuddy/debug/kvm_pool_rollout/2026_11_21_kvm_worker_placement.md#matched-ssd-and-hdd-work-disks-on-wyrm2)

| Worker and fixture state      | SSD input / setup / process | HDD input / setup / process | Image imports                                             |
| ----------------------------- | --------------------------: | --------------------------: | --------------------------------------------------------- |
| Cold work disk, fresh K3s     |        72.4 / 246.8 / 518.4 |     159.2 / 918.5 / 1,457.8 | 25 on each arm. Import envelope 199.1 s SSD, 876.5 s HDD. |
| Warm worker caches, fresh K3s |          0.3 / 92.1 / 344.9 |         0.6 / 209.8 / 672.5 | All 25 imports skipped on each arm.                       |

The 25 OCI layouts include **2.43 GiB** of stored layers deduplicated by digest, including layers
already stored as uncompressed tar. Deduplicating the measured manifests by digest within each
group leaves 1.14 GiB across the 11 PKI, KubeVirt and Kyverno images, and 1.30 GiB across fixture
and guest images, with 971 bytes shared between those groups. Sharing operators could avoid
staging their inputs into each new worker. Fixture and guest images still need delivery. The
approximately 16.4 GiB of accumulated QEMU work-disk writes in the cold HDD observation cover boot, input staging and
fixture activity. The warm HDD run shows a remaining storage penalty even when every import is
skipped. Keep the production worker disks on SSD while the required retained suite's media
sensitivity and fleet tail remain unmeasured.

The required SSD suite's
[retained-fixture trial](../../../devinfra/buildbuddy/debug/kvm_pool_rollout/2026_11_21_kvm_worker_placement.md#required-vm-suite-with-the-retained-fixture-on-ssd)
used the same BuildBuddy action across cold and proved warm on one worker.
TestRunner took 508.9 s cold and 357.2 s on reuse.
QEMU work-disk writes fell from 7.79 to 1.07 GiB,
with about 6.61 GiB saved during fixture setup. Setup still took
65.7 s and wrote 0.43 GiB
on reuse. Image streams went from 25 to zero. The baseline API, KubeVirt and PKI
controllers stayed alive. Per-run workloads and credentials were renewed.
These observations favor retaining compatible fixtures. They leave fleet hit
rate, required-check feedback and storage-media sensitivity open.

A previous same-worker recycling qualification measured 144.1 s setup and 309.9 s process time on
its cold retained seed, 41.3 s and 203.8 s on the first warm reuse, and 104.7 s and 238.3 s on the
ordinary non-recycling control. Process and node IDs, controller UIDs and the Docker-runner receipt
chain established warm reuse. Workload, PostgreSQL and credential identities changed. The
sequential sample and one ordinary action alongside the paused runner establish feasibility.
They leave natural scheduler affinity, fleet hit rate, memory headroom at load and storage effects
on retained runners open.
[Recycling qualification and BuildBuddy receipts](../debug/kvm_docker_recycling.md#higher-cap-follow-up) ·
[Production PR observations](https://agentydragon.github.io/ducktape/latency/#cases)

The separate KVM shared BuildBuddy cache trial measured staging the Firecracker target's input
tree. Warm shared proxy took 9.29 s and direct Cloud took 8.63–8.71 s. It was rolled back
after the egress-fault and recovery checks. This changes the Bazel input download path. An
executor file-cache hit alone still leaves a cold K3s image store to import and unpack those
inputs.
[Shared CAS trial](../../../devinfra/buildbuddy/debug/kvm_shared_cache_trial.md)

## Options

| Option                                                 | What it saves or preserves                                                                                                                                                                                                                           | Cost, constraint or evidence still needed                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fresh K3s per action, warm image store where available | Whole fixture remains self-contained. Warm store skips exact usable images. Provides the cold-bootstrap control.                                                                                                                                     | Restarts the API and reinstalls and waits for operators. Cold disk pays all imports. Compare clean-restart and cold-disk rates separately.                                                                                                                                                                                                                            |
| Retain K3s inside the executor                         | Current required route. Reuses the API, operators and unpacked images on a warm runner while exercising fresh workload and guest lifecycles.                                                                                                         | Requires compatible runner affinity, RAM and ext4 space. New or dirty runners rebuild. Track the roll, mismatch and eviction tails.                                                                                                                                                                                                                                   |
| Seed a fresh executor's image store                    | A digest-keyed seed for pinned infrastructure images could save some cold unpacking while preserving the private API and current test privileges.                                                                                                    | Test speculative savings against the cost to publish, fetch, mount and invalidate the seed. Application and guest images still follow the revision. Cap disk use and validate the restore as strictly as a retained store.                                                                                                                                            |
| Dedicated persistent test K3s with shared operators    | Parked prototype: amortizes K3s, KubeVirt, Kyverno and PKI while keeping their API and settings separate from production. A trusted allocator modeled namespaces, quotas, short-lived observer tokens and a capacity lease.                          | Its trusted privileged node container shared the backing host kernel. Application deployment, immutable image delivery, driver handoff, live admission/runtime and storage/TTL cleanup remain open. Revisit node isolation and capacity before letting PR jobs in. [Parked prototype](../x/shared_cluster/README.md) · [Tenant contract](../x/shared_cluster/SPEC.md) |
| Use the main cluster's KubeVirt and PKI                | May avoid nested K3s and nested KVM on bare-metal nodes. Shared operators and node image caches could amortize bootstrap and cold image work. Disposable tenant workloads could still exercise real launcher admission, volumes and guest lifecycle. | Needs a different fixture deployment, policy, networking, storage and image-delivery boundary. Existing tenant runs would share controller versions, admission availability, node pressure and capacity with production. Needs independent validation before use.                                                                                                     |
| Virtual API in a Pod                                   | Separate test CRDs, API and RBAC from the main cluster API. In a shared-node design, host KubeVirt and the host node still run the VMs. With dedicated agents, the API Pod and KubeVirt data plane can have independent lifetimes.                   | No verified open-source host-KubeVirt sync path for this fixture. Sync adds ownership, admission, status and cleanup translation. Dedicated agents still need the nested or dedicated kernel, KVM, provisioners and image store. See below.                                                                                                                           |
| Move eligible behavior tests off Kubernetes            | Keeps cluster-backed checks where Kubernetes, launcher and volume behavior is part of the contract. Guest or standalone Runtime tests can exercise selected harness, protocol and process behavior without deploying the stack.                      | Preserve system coverage for Sandbox and VMI reconciliation, admission and egress identity, virtiofs and block storage, and ACPI deletion. Measure a test split on matched PRs after deciding which assertions each lane owns. [Standalone Runtime E2E](../../env/e2e/README.md)                                                                                      |

## Virtual API in a Pod

Agentless K3s can host the API without kubelet, containerd or CNI. Upstream marks that
mode experimental and documents the need for an egress selector when connecting to workload
webhooks. A separate agent still has to run the real Pods, KubeVirt device plugin and guests.
Reusing this API could move its startup off the critical path. Image storage and unpacking still
belong to the worker running the Pods. Apache-2.0 k0smotron is another self-hosted option.
It manages a child k0s control plane in Pods and joins separate workers. The child would
install its own KubeVirt and PKI. It preserves one real API and owner chain, with extra
operator, worker and storage lifecycle to run. Prefer the existing K3s prototype if the
goal is a single persistent test cluster.
[K3s agentless servers](https://docs.k3s.io/advanced#running-agentless-servers-experimental) ·
[k0smotron source and license](https://github.com/k0sproject/k0smotron) ·
[k0smotron worker joins](https://docs.k0smotron.io/stable/join-nodes/)

vCluster's shared-node design runs the virtual control plane in a Pod and synchronizes selected
objects to the host API. Host KubeVirt reconciles host-side VM and VMI objects, creates the
launcher and uses the host's device plugin and KVM. This isolates API state, while workloads
share the host node's kernel and rely on host admission and RBAC after translation. The built-in
KubeVirt integration requires Enterprise and is outside the no-payment constraint. Generic custom
resource sync is offered on the no-cost Platform-licensed tier and compiled out of the OSS image.
Generic sync copies a CRD schema and mirrors namespaced writes asynchronously. It supports
reference patches. KubeVirt's interactive console is an aggregated API subresource outside
ordinary CRD sync. Host-generated VMIs and launcher Pods have host UIDs, while the guest identity
and launcher checks require the live host `Pod → VMI → VM → Sandbox → WorkloadCredential` chain
and its accepted VMI binding. A CRD mirror alone gives no evidence that host-generated objects,
owner UIDs, status, Secret/PVC references and finalizers make a correct round trip. Moving the
identity-sensitive controllers to the host to avoid that translation would retain most of the
direct design and add a syncer. Keep generic sync open if another use justifies a virtual API.
[vCluster architecture](https://www.vcluster.com/docs/vcluster/overview/architecture) ·
[KubeVirt integration and admission](https://www.vcluster.com/docs/vcluster/integrations/kubevirt) ·
[OSS and Free tier licensing](https://www.vcluster.com/docs/vcluster/overview/oss-vs-free) ·
[Generic sync behavior](https://www.vcluster.com/docs/vcluster/manage/custom-resources) ·
[KubeVirt console authorization](https://kubevirt.io/user-guide/user_workloads/accessing_virtual_machines/)

## Main-cluster trial gates

Read-only inspection on 21 November confirmed the main cluster's KubeVirt was `Deployed`.
The VM, VMI and Certificate APIs were namespaced, while trust-manager `Bundle` and Kyverno
`MutatingPolicy` were cluster-scoped. The main KubeVirt configuration enables virtiofs and host
devices, including a permitted USB class. The local Piraeus SSD class uses
`WaitForFirstConsumer` and `Delete`. These observations establish available primitives. A
shared fixture still needs these gates:
[Main KubeVirt configuration](../../../cluster/cdk8s/kubevirt/app.py) ·
[Storage classes](../../../cluster/docs/piraeus.md#storage-classes)

- **Authority.** A trusted allocator outside PR code owns the namespace set, quotas, policy and
  cleanup. Give the driver an application credential and a separate short-lived namespace
  observer with explicit reads and diagnostics. The current driver uses the inner admin
  kubeconfig. The chart and cleanup also handle cluster-scoped RBAC, issuers, bundles and
  launcher policies, plus a fixed collector namespace. Shared operators need preinstalled,
  trusted policy and per-run issuer and trust wiring. Test controllers need their own scoped
  identities. Tests of cluster RBAC, CRDs, operator upgrades or admission changes need the
  isolated API. Both VM cases read a fixture Secret to check its metadata and public certificate.
  Preserve those assertions through trusted observation of the run's public certificate and
  ownership metadata. Keep production credentials, workload service keys and registry-write
  authority outside the driver.
  [Fixture chart](fixture_chart.py) · [Fixture lifecycle](stack.py) ·
  [Runner certificate assertions](../scenarios/runner_credentials.py) ·
  [Kubernetes RBAC guidance](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)
- **Runtime admission.** Enforce approved Pods, VM and VMI shapes and images, including
  independently submitted VMIs. Apply Pod Security and trusted policy that restrict host
  mounts, host namespaces, privileged containers, host device passthrough, hook sidecars
  and arbitrary service-account token or Secret access. Permit only the required launcher
  behavior and verify its controller and owner chain when injecting the relay. Lock tenant
  scheduling and deny traffic to other runs, production and node services through the actual
  cluster network policy. Give each run narrowly scoped paths to its scripted model endpoint
  and trace collector so it can retain the existing turn and trace assertions.
  [Launcher policy](../../../cluster/cdk8s/agentplane/launcher_relay.py) ·
  [Kubernetes multi-tenancy](https://kubernetes.io/docs/concepts/security/multi-tenancy/)
- **State and failure isolation.** Use provider-managed per-run filesystem and raw-block PVCs
  with proven delete/reclaim. Today the fixture allocates host loop devices and creates
  local PVs itself. Put CPU, RAM, storage and object quotas on each run, and enforce an aggregate
  limit on concurrent VM boots and outstanding test leases. Reap abandoned runs by UID, verify
  volumes and finalizers, and retain capacity while cleanup is stuck. A shared webhook outage,
  operator roll, exhausted node or slow provisioner can delay every run, so test those failure
  modes in the disposable environment and keep the isolated path available.
  [Current block provisioner](../vm/block_storage.py) ·
  [Prototype allocation and cleanup](../x/shared_cluster/SPEC.md#run-allocation-and-cleanup)
- **Reproducibility.** Use immutable image digests from the tested revision, with delivery
  owned by trusted infrastructure. The fixture's `ducktape-ci/…:unset` tags only work in its own
  node image store. Shared node caches can still miss after scheduling or GC. Count publish,
  pull, unpack and readiness separately. The external Kubernetes API, CNI, storage, CRDs and
  operators would become inputs to pass/fail and Bazel cache validity. Pin or record the
  relevant versions and use fresh executions for the comparison.
  [Fixture image inputs](../k3s/defs.bzl) · [Import checks](README.md)

## Proposed first shared-fixture comparison

Start with direct KubeVirt in a disposable cluster, with a single leased run from a reviewed
revision. This tests the proposed main-cluster topology without introducing a resource syncer or
giving a PR workflow cluster access. Keep the required Bazel lane as the comparison baseline. Its
[measured SSD costs](#costs-by-fixture-state) establish the phases to compare.
If a separate API and kernel boundary remain necessary, use a dedicated K3s worker VM and
share its operators across runs. A privileged K3s node Pod shares its backing host kernel.

Begin with an offline render and permission inventory. Assign every cluster-scoped object,
issuer reference, firmware and volume reference, and driver API call to a trusted installer
or to a tenant identity. Carry only the required VM cases into the disposable comparison.

1. **Publish an exact fixture.** Build the existing fixture applications, Runner disk, boot
   kernel and guest root from one revision. Have a trusted publisher copy the declared OCI
   layouts to a test registry and return manifest digests. Render a shared-fixture profile
   with typed run namespaces, storage classes, test issuer/trust, and endpoints. Pin those
   digests in Deployments, templates, the VM kernel boot, container disks and relay. Record the
   approved manifest bundle and operator versions alongside the revision. The allocator
   applies approved fixture resources. Verify observed Pod and launcher init-container image
   IDs against the published layouts.
   [VM image groups](../suites/BUILD.bazel) · [Fixture recipe](../suites/suite_recipe.py)
2. **Prepare one tenant.** Reuse the parked allocator's Lease and namespace UID model. Make
   application, egress credentials, delegated Kubernetes accounts and tracing addresses
   run-scoped. Apply CPU, memory, ephemeral storage, PVC and VM/VMI count quotas, image and
   storage allowlists, KVM test-node placement and default-deny network rules before any
   workloads. Wire only same-run App, Dispatcher, model, OTLP, DNS and necessary Kubernetes
   API paths. The present renderer discards Cilium policies for bare K3s. Render explicit test
   policy for the host CNI. Keep host-sensitive exceptions limited to the verified KubeVirt
   launcher and its required devices and mounts. Provision raw block for workspace and
   Filesystem for state, with reclaim proven on both.
   [Allocator](../x/shared_cluster/allocation.py) · [Bare-K3s finalization](fixture_finalization.py)
3. **Keep cluster authority out of the job.** Start pinned KubeVirt, cert-manager and Kyverno
   once. The trusted installer owns CRDs, supported storage classes, and namespaced/cluster
   admission. The workload credential issuer hardcodes the `ClusterIssuer` kind. For the
   disposable comparison, use a unique test-only issuer and CA from the installer, restricted
   to the run's Certificate requests. Mount the run's public trust bundle explicitly. The
   fixture and production reuse service SPIFFE URIs, so they need separate issuer CAs and
   trust bundles. The Policy Controller watches ClusterRoleBindings and its chart grants
   cluster-wide writes. The VM fixture's tool grant is a namespaced RoleBinding.
   Prove the reviewed controller can run with required reads and without cluster-binding
   writes in the disposable lab. For production, give PR-built controllers a namespace-only
   reconciliation mode and scoped RBAC, or keep that controller trusted and record the coverage
   difference. Tenant RBAC and admission must also reject production issuer references.
   [Credential record issuer](../../identity/credentials/issuer/certificate.go) ·
   [Policy Controller manager](../../policy_controller/cmd/main.go) ·
   [Certificate admission](../../../cluster/cdk8s/agentplane/workload_certificate_admission.py) ·
   [Policy Controller RBAC](../../../cluster/cdk8s/agentplane/policy_controller_rbac.py)
4. **Run both existing cases from a Job.** Add a deployment adapter for the existing driver and
   cases. An unprivileged in-cluster Job uses the fixture's App token and an independently
   issued, short-lived fixture-CA client credential for the allowed App role. Its API account
   observes its own Sandbox, VM/VMI, Pod status and logs. The current driver reads the App
   service Secret and port-forwards to Dispatcher. Connect to the fixture's service DNS
   with the Job's projected client credential. Keep the scripted model and trace receiver
   reachable only within the run. Preserve public certificate and owner checks through trusted filtered
   evidence. Follow `guest-console-log` by actual launcher Pod UID before deletion and require
   the same supervision and ACPI shutdown markers. Interactive `virtctl console` is a separate
   read/write terminal stream. Keep it out of the driver role. Keep node-level diagnostics
   available in the isolated Bazel lane.
   [Existing cases](../suites/test_vm.py) · [Dispatcher transport](../driver/dispatcher_transport.py) ·
   [Guest shutdown proof](../vm/guest_logs.py) · [App-driven cleanup](../driver/app_sandbox.py)
5. **Prove isolation and reclamation before comparing time.** Inspect exact effective RBAC
   and admission in the disposable environment. Require allowed launcher creation, verified
   KVM, raw-block and virtiofs semantics, both turns with the existing trace checks, successful
   graceful deletion, and no cross-tenant reads, credentials, traffic or sensitive workload
   admission. Run an interrupted-driver case: a trusted reaper stops owned Sandboxes through the
   App API while their controllers still run, then deletes the recorded namespace and cluster
   object UIDs. Keep the lease until VM, VMI, Pods, claims and underlying storage are gone.
   Fail an incomplete or forced shutdown rather than count cleanup as passed.

Then compare cold and warm node caches and two overlapping leases against a warm retained
BuildBuddy action at the same revision. Count image build/publish, lease wait, pull/unpack, app
setup, guest readiness, scenarios, diagnostics and cleanup separately. Verify candidate changes
against the recorded operator, policy and storage versions. The shared route has no measured
speedup and its operational cost includes the publisher, allocator, evidence collector and
reaper. A future required check would need an explicit cache key for external fixture inputs or
fresh execution and a CI signal for infrastructure failure. The initial comparison is manual
and keeps the existing PR coverage in place.
