# System test deployment

This package owns fixture charts and rendering, fresh application workloads, database and identity
fixtures, per-action tracing, and the pytest deployment adapter. Retained node setup belongs to
[`k3s/`](../k3s/README.md); VM runtime, storage and guest images belong to
[`vm/`](../vm/README.md). The guide below describes how these parts compose into a running stack.

The disposable stack used by the [CI system test suites](../README.md): the Agentplane services on a
bare k3s inside the suite's own RBE action (no Cilium), which PR CI runs like any test.
[`system_test_target.py`](system_test_target.py) is the pytest plugin that starts it and publishes it as
the deployment under test, with LLM ingress forwarding to the scenario's scripted model on the node.
Pod suites include the deployment package's `environment_tools_harnesses_image`, which carries both pinned harnesses.

containerd keeps unpacked image layers (its overlayfs snapshotter's root) on a tmpfs, except on a stack
with VM Sandboxes ([measurements](../k3s/debug/stack_timings.md#image-layers-in-memory-2026-10-21)).
The stack pins its imported OCI images in containerd's CRI metadata. Kubelet image GC can otherwise
delete an image before a later Sandbox uses it; these fixture-local tags cannot be pulled from a registry.
At import-group boundaries, the stack removes untagged, unpinned CRI images
that no container uses. Named caches stay warm, including k3s's own images. Before importing, it also
removes unused fixture-local images (`git.allegedly.works/ducktape-ci/…`) the target's manifest does
not declare: Pod and MCP suites share a recycling key, and pinned MCP layers would otherwise stay in a
Pod stack's tmpfs. An MCP suite after a Pod suite reimports them. Cleanup also runs with
the kubelet stopped during a cold rebuild, before restored obsolete layers can trigger evictions
while a fresh kubelet waits for its image-GC minimum age. See the
[storage-pressure reproduction](../k3s/debug/2026_11_02_image_store_pressure.md).
During imports, using half of a group's starting filesystem headroom triggers an intermediate
sweep. Filesystem checks avoid a CRI round trip for each unchanged or small image import.

Every `k3s_suite` retains live k3s and cert-manager/trust-manager in hosted BuildBuddy RBE snapshots. VM suites
also retain KubeVirt and Kyverno. This is the default for Pod, Dispatcher, all MCP suites,
normal/manual/profile VM suites and real-model tests; there are no separate persistence variants.
The existing manual/live tags still determine which suites run in CI, and real-model tests require
`OPENAI_API_KEY`.

Retention requires `AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1`, injected by BuildBuddy's
`test.env-overrides` exec property, rather than the test's ordinary environment. Without that
executor permission, the stack starts fresh infrastructure and stops k3s during teardown, including
when setup or a scenario fails. Local BuildBuddy credentials and `CI` do not enable retention.
If an action without permission restores a previously retained snapshot, it first stops the server
identified by the ownership marker, then starts and tears down its own fresh node.

Pod/MCP/real-model suites share one tmpfs snapshot family; VM suites share a separate disk-backed
family with the VM resource estimate. Baseline fingerprints include the layer mode, k3s, CRDs,
`baseline.py`, its bootstrap, iptables and release helpers, `pki_infrastructure.py`,
`certificates.py`, and PKI releases/images. VM stacks also include `vm/infrastructure.py` and VM
releases/images/configuration. `stack.py` owns fresh workload orchestration and cleanup;
`vm/runtime.py` owns per-action VM volumes and guest evidence. The fingerprint includes
`certificates.py` because its cleanup determines whether retained PKI is clean.
Only VM suites load `vm/pytest_plugin.py`, which supplies the typed lifecycle extension; Pod/MCP
runfiles omit VM runtime helpers.

A missing, unhealthy, dirty or incompatible baseline rebuilds cold. Snapshot publication follows BuildBuddy's default save policy; suites do not force a save after each action. Until a
refreshed snapshot is saved, later actions can repeat the rebuild.

Snapshot family keys derive from the k3s binary, PKI release/chart/Helm checksums and
PKI image digests. The VM key also includes KubeVirt/Kyverno release checksums and image
digests. OCI pins are owned by `devinfra/image_pins.json`; HTTP artifact pins remain in
`MODULE.bazel` and export their checksums through `devinfra/artifact_pins.bzl`. Repinning
retained infrastructure automatically selects a new family. Application, runner and guest
images remain per-action inputs. The runtime fingerprint additionally checks actual
baseline contents, helper code and CRDs before reuse.

Applications, PostgreSQL, model endpoints, MCP fixtures, certificates/trust and collectors are recreated.
Fixture Certificates rendered outside the deleted fixture namespaces are removed with their
CertificateRequests selected by Certificate UID and their declared Secrets. This refreshes the
fixture root and workload CA while preserving unrelated PKI resources and the retained controllers.
VM workspace claims, PVs, StorageClass and loop devices are removed before marking the baseline clean;
guest shutdown checks and the selected boot/profile settings are preserved. k3s and the retained
controllers remain running after successful teardown.
See the [persistence evidence](../../debug/k3s_persistence.md).

Setup, each image import and teardown use the same OpenTelemetry tracer as the scenarios,
writing completed spans immediately to `test-spans.jsonl` in undeclared outputs. The test log prints
a summary derived from these spans. See [trace artifacts](../README.md) for stage coverage.
Retained baselines also publish bounded tails of the k3s, containerd and containerd teardown logs
as `*-retained.log`, plus `baseline-transitions.jsonl` with reset/stop ownership and file-state evidence,
including when baseline attachment fails. Dispatcher failure diagnostics include
CRI image status and containerd image references before the failed Sandbox is deleted.
`storage/{before-import,final}/` captures filesystem bytes/inodes, CRI and containerd images,
kubelet stats/config/metrics, and Node/Pod events. `image-prune.log` records reclaimed image IDs;
`system_test.images.prune` spans account for cleanup time.

Every stack exposes the fixture-owned OTLP receiver at the production Alloy service name, so
service and Runner spans are collected in `process-spans.jsonl`. Driver spans write directly to disk
so setup can be traced before the cluster/receiver exists. The two transports retain trace/parent IDs;
the process receiver filters attributes to keep credentials and request content out of artifacts.
The bundled containerd does not register its upstream tracing plugin; imports have driver OTel spans
and the existing Go CPU/runtime profiles.

Suites call the app as the fixture's test operator, with the OIDC ID token in `Stack.token`. A plain
fixture names a test-only issuer under the reserved `.test` domain, and the app pins a key the stack
generates (`oidc.static_ed25519_keys`); the test process signs the token, so no issuer runs. MCP
fixtures keep Dex as the app's issuer, and the stack takes the operator's token from Dex's password
grant for the app's client ([`operator_tokens.py`](operator_tokens.py)).

A suite using the MCP fixture stack takes the `rendered/local-model-mcp-fixtures.json`
rendering and also imports and runs Dex, `mcp-everything` and the OAuth MCP fixture, with
test-only Dex clients, operator, MCP server and Action groups. The
standalone MCP edge serves its OAuth and MCP endpoints in this fixture; Actions keeps its separate
OAuth client for the upstream fixture used by `mcp:test_actions`. Dex
reads a config Secret holding the bcrypt hash of a fixed test-only operator password.
Database passwords and OIDC client/session secrets are fixed test-only literals; Dex uses a fixed
bcrypt salt so its hash is repeatable. The rendered JSON is a Bazel build output, not checked into git.
`rendered_fixture.fill` supplies the runtime TLS material, model endpoint and forwarded origins.
Dex serves on its in-cluster Service name, which the test process resolves through `/etc/hosts`.
It serves TLS because the app and edge use a plain-http OAuth origin only on loopback: a CA made per
stack signs its certificate, and the app, Actions and edge trust that CA. In MCP fixtures the app and
edge use separate loopback HTTP origins, which `stack.py` port-forwards to their Services. The app
login callback and MCP linkage callback use the app origin; the edge's upstream OAuth callback uses
the edge origin. Each satisfies endpoint validation without allowing insecure non-loopback URLs.

The stack's tests drive the app's REST/SSE API and omit Electric and the browser frontend. Tests of the
frontend sync protocol run Electric separately. The database bootstrap still creates the `electric`
role required by the existing migrations; it does not start an Electric process.

k3s runs its bundled kube-proxy, so the fixture's Services are ordinary ClusterIP Services and Pods
use cluster DNS and the `kubernetes` Service.
**Gotcha:** kube-proxy needs the iptables `MARK` target from the executor's guest kernel, which no
image change supplies. The executor lacked it until 2026-10-21 and [#10808](https://github.com/agentydragon/ducktape/pull/10808)
was reverted for that; only a k3s target's run, never a green `test_render`, shows an executor
can run it.

[`fixture_chart.py`](fixture_chart.py) constructs the chart directly
with shared service constructs and explicit bare-k3s settings. It supplies fixture Secrets,
local-path storage for filesystem claims and a test egress CA. VM stacks add the
`agentplane-test-loop-block` class for workspace claims. [`block_storage.py`](../vm/block_storage.py) runs a
host-side provisioner in an owned thread, independent of pytest's per-test event loops. It relists
claims in the stack namespace and creates one sparse 10 GiB file, loop device, and UID-reserved
`Block` PV for each matching claim. Volumes survive claim deletion; a replacement UID gets fresh
storage. The provisioner stops before k3s, and its devices detach after k3s removes its Pods.
Worker failures propagate to fixture teardown. This gives the VM test guest a real raw block
device while keeping state on the fixture's filesystem class. The fixture runs the Go egress service through the same construct as staging, with fixture-only
PostgreSQL TLS settings. Its serving process applies the embedded Goose migrations before opening
listeners. The shared construct supplies the egress RBAC, policies, trust mounts, images and flags.
Actions and Notifications apply Goose migrations from their serving processes before opening
listeners or starting workers. Their startup probes wait for initialization; the fixture renders no
separate migration containers for either service.
The fixture selects that runtime explicitly; staging selects its own independently.
The standalone MCP edge runs in every stack because the app's Connection Service endpoint targets it;
its own PostgreSQL database, embedded Goose migrations, caller-token RBAC and startup/readiness
probes are rendered through the shared construct. The serving binary migrates before opening its
gRPC listener. MCP fixture stacks add the edge OAuth Secret and Dex trust.
One PostgreSQL 18 Deployment uses the
pinned published CNPG minimal image, an emptyDir data volume and
[`postgres_entrypoint.sh`](postgres_entrypoint.sh) with [`bootstrap.sql`](bootstrap.sql) to create
the service-owned databases. Unsupported Cilium, Gateway API, Prometheus and External Secrets resources are omitted during
construction. VM stacks install Kyverno for the launcher relay. The chart instantiates the Go Runner Gateway
directly with one gRPC listener on port 8080; the stable `runner-gateway` and `session-dispatcher`
Service names remain. The serving process applies the embedded Dispatcher migrations before opening
the listener, using the Dispatcher database Secret. [`stack.py`](stack.py) starts k3s, streams
the Bazel-built OCI image layouts into containerd, applies the rendered stack and reports Deployment
readiness. It uses the pinned K3s binary from Bazel runfiles, so no action downloads K3s at runtime.

Every stack explicitly uses mTLS for runner control and installs real cert-manager and trust-manager from pinned upstream manifests/charts and
Bazel-pulled OCI layouts. Startup probes admission through both webhooks before applying the fixture.
A self-signed bootstrap ClusterIssuer issues the test root CA; the production runner Certificate/Issuer
constructs issue the intermediate CA and Gateway leaf. Sandbox Controller creates each runner's
Certificate. trust-manager distributes the root through a Bundle into the workload namespace;
startup waits for certificate readiness and Bundle synchronization. Controller logs and PKI resource
status are retained under `certificates/` in undeclared test outputs. Optional insecure transport is
limited to standalone behavior fixtures; k3s has no insecure provisioning profile.
`test-spans.jsonl` records controller readiness and certificate/trust readiness for startup-cost analysis.

Node readiness and the appearance of the bundled DNS/storage Deployments share a 180-second
bootstrap deadline. API probes have bounded subprocess timeouts and stop when k3s exits;
bootstrap failures include the last probe result and k3s log tail and stop the server process.

The image list includes the serving binaries, which apply their embedded migrations at startup. A fast
[`test_render`](BUILD.bazel) check validates this fixture composition and its endpoint/Service wiring
without starting Kubernetes.
`k3s/defs.bzl` declares each suite's image references directly alongside its image targets. A small
analysis rule writes the selected fixture, PKI, VM and guest mappings into one JSON manifest and
carries those layouts in runfiles. The suite owns these artifacts; importing the stack library does
not bring in service images or k3s. The importer
combines index descriptors with those references and streams each distinct blob once, without
materializing Docker-format `oci_load` tarballs.

One background importer loads the next image group while startup applies and waits for the preceding
group: PKI during KubeVirt installation, service images during PKI readiness, and guest images during
fixture application. Each workload waits for its own images before being applied, preventing registry
pulls. Imports remain serial, and the importer joins before k3s teardown even when startup fails.

The Pod runner uses the production `//agentplane/env/images:disk_image` volume at `/run/agentplane/environment`, with the
`environment_tools_harnesses_image` for the rest. It contains the pinned Claude Code and full Codex distributions.
VM-only targets omit the Pod runner image:
their Sandboxes use the runner disk and test guest images. Image volumes need containerd 2.1 or later and the
`ImageVolume` feature gate, which `stack.py` turns on in the API server and kubelet.

## Egress authentication matrix

SPIFFE over TLS is the only Sandbox egress authentication after the service cutover. The auth
selector is removed; environment kind does not select an auth mode. This is the exercised matrix:

| Fixture                   | Environment   | Egress authentication |
| ------------------------- | ------------- | --------------------- |
| `local-model`             | Pod           | SPIFFE over TLS       |
| `local-model-vm`          | QEMU/KubeVirt | SPIFFE over TLS       |
| `local-model-firecracker` | Firecracker   | SPIFFE over TLS       |
| `public-litellm`          | Pod           | SPIFFE over TLS       |
| MCP fixtures              | Pod           | SPIFFE over TLS       |

## RBE executor constraints

Probed 2026-10-21 with `bb execute` under the k3s target's isolation properties: kernel 5.15.0, no `/dev/kvm`
(`CONFIG_KVM` unset, only `CONFIG_KVM_GUEST`) and no BPF JIT. So the stack runs no Cilium, KubeVirt only with
software emulation, and kube-proxy with ClusterIP Services.

The earlier RBE precompile failure no longer reproduces with `rules_python` 2.4.3: a target using
`pyc_collection = "include_pyc"` passes and Python loads the generated `.pyc` files. The
[current experiment](../k3s/debug/pyc_precompilation.md) shows the large compile action graph and the
observed warm-run difference, so the suites do not enable collection yet. With the chart rendered at
build time, imports still took 10–13 s before pytest in the October 21 measurements
([measurements](../k3s/debug/stack_timings.md#fixture-chart-rendered-at-build-time-2026-10-21)). Worker speed dominates
run-to-run variance ([per-worker timings](../k3s/debug/stack_timings.md#phase-timings-and-image-bytes-2026-10-21)),
so a time budget has to hold on slow workers.

## Sandbox lifecycle coverage

The model-free storage scenario in `test_pod` creates a Sandbox and waits for its own `Ready=True` and
`Bootstrapped` conditions, reads the markers its bootstrap wrote, suspends it until the Pod is gone,
resumes it with its state and workspace volumes unchanged, and deletes it until it and its claims are gone.
The controller gates Ready on workload readiness, runner connection and bootstrap success.

Exact bootstrap and seal-before-stop ordering belongs in deterministic
[controller tests](../../sandbox_controller/controller_test.go), including
`TestReadyWaitsForTheBootstrapAndStopsOnItsFailure` and
`TestSuspensionWaitsForTheRunnerToQuiesce`.

## VM Sandboxes

VM stacks, `test_vm` and the opt-in `test_vm_manual` and `test_vm_oom`,
([system tests README](../README.md)), run on a stack rendered with VM Sandboxes
(`rendered/local-model-vm.json`), so [`sandboxes.py`](../vm/sandboxes.py)
installs production's KubeVirt release with `useEmulation` (QEMU TCG; the executor has no `/dev/kvm`)
and Kyverno's admission controller, which adds the egress relay to each launcher Pod as in
production. The fixture applies once KubeVirt's VirtualMachine API is served: the Sandbox Controller
looks for it only at start. The stack writes KubeVirt's and Kyverno's view of the VMs, each guest's
serial console and any image a Pod pulled from a registry to `test.outputs/vm-sandboxes.txt`.
The manual `test_vm_profile` variant adds a perf-capable guest root for native app-server
startup profiling; its runtime opt-in, repeat commands, and artifact path are in the
[native startup profile note](../../debug/2026_10_31_vm_native_startup_profile.md).

The template boots the [test guest](../vm/guest/BUILD.bazel) instead of production's NixOS image, which sits
in a private registry and would boot for minutes without KVM: a Debian cloud kernel that KubeVirt boots
directly, a BusyBox initramfs that mounts an erofs root under a tmpfs overlay, and real systemd as
PID 1. The guest installs the same runner service and resource slices as
[`vm.nix`](../../env/images/vm.nix), with fixture-specific tool paths and mount/network preparation.
The root image includes a hardware database compiled from its Debian package closure, letting
systemd skip rebuilding it at each boot before starting udev's device discovery.
The runner comes from the disk mounted at `/run/agentplane/environment`; Pod suites use the
production disk, while VM suites select its test variant. That variant adds
`/run/agentplane/environment/bin/environment-runtime-systemd-fixture`, leaving the Debian guest root
independent. Both harnesses, Git and `findmnt` are pinned in the root disk. Runner uses systemd's
private manager connection; a real system bus also exercises denial of unprivileged management
requests.

The Claude sandbox scenario uses `environment-vm-supervision`, which differs from `environment-vm` only by
an explicit boot argument. That boot runs
`/run/agentplane/environment/bin/environment-runtime-systemd-fixture` from the test runner disk before
starting production Runner.
The checks retain direct argv/stream semantics, stale invocation fencing, uncertain launch outcomes,
scoped startup fencing, parent death, stop status and reaper retries. They need private injection or
direct supervisor access. A failure prevents Runner startup, and the scenario requires the completed
success receipt from that guest's own console. No separate root image or extra VM is needed.

Workspace ownership, native identity/security settings, systemd and kernel resource properties, and
detached-child cleanup run through ordinary Session setup and retained History in both harness
scenarios. Their turns also verify the runner certificate and identity, authenticated session,
command admission and retained History. The two turn guests and one Codex sandbox/OOM guest boot together.
The suite requests an 8-unit worker so the guests and k3s services fit without waiting for another
group to exit.

The guest loads the ACPI button/evdev drivers and runs BusyBox `acpid`. On a power-button event,
the listener requests `systemctl poweroff`. Systemd signals Environment Runtime first; Runtime
publishes its closing intent, waits for Host native drain and the Execution fence, then stops both
service units. Their mount dependencies keep workspace and state available through cleanup, after
which systemd unmounts them before powering off. A shutdown-proof unit checks that both mounts are
gone before emitting the exact console marker required by teardown. KubeVirt's
shutdown grace period and the controller's wait for actual deletion remain in place. The shared
launcher admission policy keeps PVC-backed virtiofs daemons running as native sidecars until compute
exits; otherwise Pod termination can stop the state filesystem while the guest closes its journals.
Existing KubeVirt initializers run before these daemons; ContainerPath daemons retain their ordinary
container placement for KubeVirt reconciliation.

`test_vm` starts five VM Sandboxes concurrently in one stack: one per harness for the
scripted turn, one per harness for credential authentication, and one for Codex tool OOM containment.
The OOM scenario drives the real Codex app-server to run a memory-exhausting shell command through
its isolated exec-server. The allocator
touches memory beyond the configured executor limit, with a raised `oom_score_adj` to select that
tool as the victim. The test checks a single
`memory.events` OOM kill with `memory.oom.group=0`, a tool result reporting exit 137, and a failed
native command item with that exit code; the turn itself completes normally. Runner boot identity
and app-server process identity/PID stay unchanged before and after the OOM and a successful
follow-up turn. That command runs in the same executor leaf and reports exit zero. Evidence goes
to `test.outputs/vm-codex-oom.json`, alongside the guest console and scripted upstream requests.
The OOM template explicitly sets `agentplane.cgroup-observer=1`, starting a root observer outside
the execution slices to publish read-only snapshots of executor limits and OOM counters. Other
guests retain the observer unit for the unprivileged restart-denial check without starting its
polling process ([native CPU evidence](../debug/vm_observer_scope.md)). Tool processes retain
production's UID and cgroup permissions.
The executor aggregate's hierarchical OOM counter proves the constrained subtree, while the leaf's
victim counter proves the single kill: either the aggregate or leaf limit can trigger first.
This exercises the test guest's kernel, shared production systemd units, and production Runner/Codex
binaries; it does not boot the production NixOS image or force exec-server death/restart.

Only this OOM sandbox selects the cloned `environment-vm-codex-oom` template to enable the guest
cgroup observer. Its guest RAM and all resource budgets match `environment-vm`, including the
256 MiB native executor leaf. The command grows above that limit so the kernel exercises the OOM
path while the Codex session remains active; see [the measured budget probe](../debug/vm_oom_budget.md).

KubeVirt's and Kyverno's images are Bazel-pulled layouts (`vm_sandboxes/`, pinned in `MODULE.bazel`),
imported before their releases are applied, so no Pod pulls them; the releases roll out while the
fixture images import. Fixture images (services, shared runner and optional MCP fixtures)
and VM guest images are collected separately. Both guest boot and root images import after the fixture
manifests are applied, while their Deployments start. The stack finishes those imports before yielding
to a test that can create a VM Sandbox. Deployments must use fixture images, never guest images.

## Gotchas

- **Kernel sysctls:** the kubelet needs every `/proc/sys/kernel` entry present in the stack's shadow copy, not
  only the keys it sets.
- **Disk:** the images need about 1 GiB each in containerd; the default 12 GiB disk fails, hence
  `EstimatedFreeDiskBytes` in `K3S_EXEC_PROPERTIES`.
- **No resource limits:** the fixture renders no container limits (requests stay); a 500m CPU limit throttled
  Python imports by about 14 s each on the shared VM.
- **Pod egress:** flannel `host-gw` does not translate Pod traffic, so the stack adds a `MASQUERADE` rule for the
  Pod CIDR using the `iptables` k3s ships.

Guest console logs are copied continuously to `test.outputs/vm-artifacts/consoles/`, scoped by
launcher Pod UID and container log inode. They retain Kubernetes CRI timestamps and stream markers,
including the guest kernel, systemd, Runner and native service diagnostics. Open log descriptors preserve
output across launcher deletion, suspension, replacement and container restarts.
On successful VM test teardown, each retained launcher incarnation must contain
`AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, emitted by a systemd unit after runner exit and successful
workspace/state unmounts. Rotated logs are checked together per launcher. Existing test failures
skip this additional assertion while retaining their logs. Launcher `compute` logs and the KubeVirt
namespace's `virt-handler` logs are retained under `vm-artifacts/launchers/` and
`vm-artifacts/handlers/`, respectively, for graceful-shutdown and domain-exit evidence.

The normal `test_vm` target also selects block-device boot evidence on its OOM guest only. An early
guest unit runs `udevadm monitor --kernel --udev --property --subsystem-match=block`; it reports
`AGENTPLANE_UDEV_MONITOR_READY` after both monitor sockets are bound and before systemd coldplug.
This captures coldplug and udev rule completion for block devices, including their serial and
`DEVLINKS`; it does not timestamp the original kernel driver registration. The guest kernel's
serial printk messages show registration, while the monitor distinguishes kernel coldplug replays
from post-rule udev events. Its event timestamps are when the monitor handles the event on guest
`CLOCK_MONOTONIC`; they are not the original device-emission time. A separate bounded `Type=exec`
report starts after `agentplane-environment-runtime.service` starts, independently of Runner
readiness and without holding
`multi-user.target` open. It records the workspace symlink and udev properties, block-device names
and serials, `/etc/fstab`, and the udevd, udev-trigger, workspace `.device`, makefs, mount and
environment-runtime unit configuration and monotonic properties. Kernel version, command line,
boot ID and service journal records come from the copied native journal rather than additional
guest probes or a filtered journal re-export. It attempts optional `systemd-analyze` timing and critical-chain output after required
sections; if the boot transaction is still finishing, those commands can fail and their statuses
remain visible without making the required report partial. Required collection shares the
service's 35-second runtime bound. Unit properties and definitions are queried in batches to avoid
repeated systemctl startup and connection costs. Every command has an exit-status marker, and
`status=partial` or a missing end marker identifies incomplete required evidence. The required
completion receipt precedes the optional analyzers, which retain their individual two-second
bounds, so an optional probe cannot prevent completed required evidence from being certified.
Machine-readable readiness and section/completion receipts are written directly to the guest serial
port. Command output, per-command status and udev events go only to the journal. This keeps receipt
parsing independent of the journal's format without duplicating bulk output on the console.

Find `AGENTPLANE_BOOT_EVIDENCE_BEGIN` in the OOM guest's retained console and the command output in
its copied journal (`journalctl --file=SYSTEM_JOURNAL --unit=agentplane-boot-evidence.service`). The
journal's `_BOOT_ID` and kernel entries identify the boot; the report also captures the DMI UUID.
The report labels its clocks separately: `/proc/uptime` is guest `CLOCK_BOOTTIME` and includes suspend;
udev monitor timestamps, journal `short-monotonic`, and systemd `*TimestampMonotonic` use guest
`CLOCK_MONOTONIC`. Kernel printk brackets are guest kernel timestamps; CRI prefixes on the outer
console artifact are host receipt times and can be buffered. The ordinary OOM guest does not
suspend during the collected boot timeline. Do not add host CRI timestamps to guest monotonic
values. The manual `test_vm_profile` and `test_vm_manual` targets do not select this collector.

For VM tests, shared client fixture cleanup exports retained Session events, including native frames
in both directions, stderr and lifecycle failures, for
every Sandbox where that client opened a Session. Exports go to
`test.outputs/vm-artifacts/native/<sandbox-uid>/<thread-id>.jsonl`, with archive cursors and origins;
the adjacent JSON records the export watermark. Failed and partially observed turns are included.
