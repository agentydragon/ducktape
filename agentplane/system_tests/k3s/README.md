# k3s system test stack

The stack every [system test](../README.md) scenario runs on: the Agentplane services on a
bare k3s inside the scenario's own RBE action (no Cilium), which PR CI runs like any test.
[`system_test_target.py`](system_test_target.py) is the pytest plugin that starts it and publishes it as
the deployment under test, with LLM ingress forwarding to the scenario's scripted model on the node.
Scripted targets take the `harnesses/runner_layout` image, which carries both pinned harnesses.

containerd keeps unpacked image layers (its overlayfs snapshotter's root) on a tmpfs, except on a stack
with VM Sandboxes ([measurements](debug/stack_timings.md#image-layers-in-memory-2026-10-21)).

The RBE platform recycles a runner after a passing action, on the same executor. A stack removes its
Pods when it ends; the next stack on that runner keeps containerd's image store, resets the rest of the
node, and streams only the blobs containerd lacks ([measurements](debug/stack_timings.md#recycled-runners-2026-10-23)).
VM system tests use a separate `test.runner-recycling-key` so their disk-backed layers and pod tests'
tmpfs layers keep separate image stores.

Each stack records its setup in `k3s-timeline.jsonl` among the test's undeclared outputs: when each
step finished and how long each image took to import, written as it happens, so a hung run shows how far
it got. A k3s target's test log ends with the same timeline.

A target declared with `mcp_fixtures = True` takes the `rendered/local-model-mcp-fixtures.json`
rendering and also imports and runs Dex, `mcp-everything` and the OAuth MCP fixture
(`mcp_fixtures/*_layout`), with test-only Dex clients, operator, MCP server and Action groups. The
standalone MCP edge serves its OAuth and MCP endpoints in this fixture; Actions keeps its separate
OAuth client for the upstream fixture used by `test_mcp`. Dex
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
local-path storage for filesystem claims and a test egress CA. VM stacks add a separate static
`agentplane-test-loop-block` class for the VM workspace claim. `block_storage.py` backs its three
10 GiB `Block` PVs with sparse files attached to loop devices on the k3s node; the action detaches
only the loops it attached after k3s removes its Pods. This gives the VM test guest a real raw block
device while keeping state on the fixture's filesystem class. The fixture runs the Go egress proxy
and Goose history migrator through the same construct as staging, with fixture-only PostgreSQL TLS
settings. The shared construct supplies the egress RBAC, policies, trust mounts, images and flags.
Actions runs Goose migrations from its service image through `/usr/local/bin/actions-migrate`,
with native gRPC readiness and `livez` probes.
The fixture selects that runtime explicitly; staging selects its own independently.
The standalone MCP edge runs in every stack because the app's Connection Service endpoint targets it;
its own PostgreSQL database, Goose init migrator, caller-token RBAC and gRPC readiness are rendered
through the shared construct. MCP fixture stacks add the edge OAuth Secret and Dex trust.
One PostgreSQL 18 Deployment uses the
pinned published CNPG minimal image, an emptyDir data volume and
[`postgres_entrypoint.sh`](postgres_entrypoint.sh) with [`bootstrap.sql`](bootstrap.sql) to create
the service-owned databases. Unsupported Cilium, Gateway API, Prometheus and External Secrets resources are omitted during
construction. VM stacks install Kyverno for the launcher relay. The chart instantiates the Go Runner Gateway
directly with one gRPC listener on port 8080; the stable `runner-gateway` and `session-dispatcher`
Service names remain. A Goose init container runs before the Go server container and uses the
Dispatcher database Secret. [`stack.py`](stack.py) starts k3s, streams
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
`k3s-timeline.jsonl` records controller readiness and certificate/trust readiness for startup-cost analysis.

Node readiness and the appearance of the bundled DNS/storage Deployments share a 180-second
bootstrap deadline. API probes have bounded subprocess timeouts and stop when k3s exits;
bootstrap failures include the last probe result and k3s log tail and stop the server process.

The image list includes the Go Runner Gateway and Goose queue migration images. A fast
[`test_render`](BUILD.bazel) check validates this fixture composition and its endpoint/Service wiring
without starting Kubernetes.
The importer consumes the `oci_image` targets through `oci_layout_rloc`, combines their index
descriptors with the rendered tags, and streams each distinct blob once. It does not materialize
Docker-format `oci_load` tarballs.

The Pod runner uses the production `//agentplane/runner:disk_image` volume at `/run/agentplane/runner`, with a
tools image for the rest. Model-free Pod targets import `:runner_image`; targets that run a turn import
`:runner_harnesses_image`, which adds pinned Claude Code and Codex. VM-only targets omit the Pod runner image:
their Sandboxes use the runner disk and test guest images. Image volumes need containerd 2.1 or later and the
`ImageVolume` feature gate, which `stack.py` turns on in the API server and kubelet.

## RBE executor constraints

Probed 2026-10-21 with `bb execute` under the k3s target's isolation properties: kernel 5.15.0, no `/dev/kvm`
(`CONFIG_KVM` unset, only `CONFIG_KVM_GUEST`) and no BPF JIT. So the stack runs no Cilium, KubeVirt only with
software emulation, and kube-proxy with ClusterIP Services.

**Gotcha:** rules_python's precompile (`pyc_collection = "include_pyc"`) fails on RBE, because the exec
interpreter does not find its standard library. Each action compiles bytecode as it imports, about three
quarters of import time; with the chart rendered at build time, imports still take 10–13 s before pytest starts
([measurements](debug/stack_timings.md#fixture-chart-rendered-at-build-time-2026-10-21)). Worker speed dominates run-to-run variance
([per-worker timings](debug/stack_timings.md#phase-timings-and-image-bytes-2026-10-21)), so a time budget has to hold on slow workers.

## Sandbox lifecycle coverage

The model-free `test_storage` scenario creates a Sandbox and waits for its own `Ready=True` and
`Bootstrapped` conditions, reads the markers its bootstrap wrote, suspends it until the Pod is gone,
resumes it with its state and workspace volumes unchanged, and deletes it until it and its claims are gone.
The controller gates Ready on workload readiness, runner connection and bootstrap success.

Exact bootstrap and seal-before-stop ordering belongs in deterministic
[controller tests](../../sandbox_controller/controller_test.go), including
`TestReadyWaitsForTheBootstrapAndStopsOnItsFailure` and
`TestSuspensionWaitsForTheRunnerToQuiesce`.

## VM Sandboxes

A system test scenario's `_vm` target ([system tests README](../README.md)) runs on a
stack rendered with VM Sandboxes (`rendered/local-model-vm.json`), so [`vm_sandboxes.py`](vm_sandboxes.py)
installs production's KubeVirt release with `useEmulation` (QEMU TCG; the executor has no `/dev/kvm`)
and Kyverno's admission controller, which adds the egress relay to each launcher Pod as in
production. The fixture applies once KubeVirt's VirtualMachine API is served: the Sandbox Controller
looks for it only at start. The stack writes KubeVirt's and Kyverno's view of the VMs, each guest's
serial console and any image a Pod pulled from a registry to `test.outputs/vm-sandboxes.txt`.

The template boots the [test guest](guest/BUILD.bazel) instead of production's NixOS image, which sits
in a private registry and would boot for minutes without KVM: a Debian cloud kernel that KubeVirt boots
directly, a BusyBox initramfs that mounts an erofs root under a tmpfs overlay, and real systemd as
PID 1. The guest installs the same runner service and resource slices as
[`vm.nix`](../../runner/vm.nix), with fixture-specific tool paths and mount/network preparation.
The runner comes from the production runner disk, as in a Pod; both harnesses, Git and `findmnt`
are pinned in the root disk. Runner uses systemd's private manager connection; a real system bus
also exercises denial of unprivileged management requests.

The dedicated CI target `//agentplane/system_tests:test_runner_systemd_vm` selects a guest root
with `runner_systemd_fixture` and an entrypoint override. Before `exec`ing the production runner,
it runs all subtests against the real manager. Its isolated `/state/.supervision-proof`
exercises native process streams, limits, descendant fencing, uncertain launch outcomes and parent
death. A failed proof prevents runner startup; its output is retained in the guest console. The
outer test requires the completed success receipt as well as runner readiness. Ordinary VM targets
use a root disk without the proof binary or override and start the runner directly. Both variants
share the kernel, production runner disk, systemd units and guest preparation. The proof target
still uses the full k3s application stack.

The guest loads the ACPI button/evdev drivers and runs BusyBox `acpid`. On a power-button event,
the listener requests `systemctl poweroff`. Systemd stops the runner and its execution services,
then unmounts the workspace and state before powering off. A shutdown-proof unit checks that both
mounts are gone before emitting the exact console marker required by teardown. KubeVirt's
shutdown grace period and the controller's wait for actual deletion remain in place. The shared
launcher admission policy keeps PVC-backed virtiofs daemons running as native sidecars until compute
exits; otherwise Pod termination can stop the state filesystem while the guest closes its journals.
Existing KubeVirt initializers run before these daemons; ContainerPath daemons retain their ordinary
container placement for KubeVirt reconciliation.

`test_vm_sandbox_vm` starts three VM Sandboxes concurrently in one stack: one per harness for the
scripted turn and one for Codex tool OOM containment. The OOM scenario drives the real Codex
app-server to run a memory-exhausting shell command through its isolated exec-server. The allocator
touches memory beyond the configured executor limit, with a raised `oom_score_adj` to select that
tool as the victim. The test checks a single
`memory.events` OOM kill with `memory.oom.group=0`, a tool result reporting exit 137, and a failed
native command item with that exit code; the turn itself completes normally. Runner boot identity
and app-server process identity/PID stay unchanged before and after the OOM and a successful
follow-up turn. That command runs in the same executor leaf and reports exit zero. Evidence goes
to `test.outputs/vm-codex-oom.json`, alongside the guest console and scripted upstream requests.
The test guest runs a root observer outside the execution slices to publish read-only snapshots of
executor limits and OOM counters. Tool processes retain production's UID and cgroup permissions.
The executor aggregate's hierarchical OOM counter proves the constrained subtree, while the leaf's
victim counter proves the single kill: either the aggregate or leaf limit can trigger first.
This exercises the test guest's kernel, shared production systemd units, and production Runner/Codex
binaries; it does not boot the production NixOS image or force exec-server death/restart.

KubeVirt's and Kyverno's images are Bazel-pulled layouts (`vm_sandboxes/`, pinned in `MODULE.bazel`),
imported before their releases are applied, so no Pod pulls them; the releases roll out while the
fixture images import. Fixture images (services, migrations, shared runner and optional MCP fixtures)
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
workspace/state unmounts. Rotated logs are checked together per launcher. Existing test failures skip
this additional assertion while retaining their logs. Launcher `compute` logs and the KubeVirt
namespace's `virt-handler` logs are retained under `vm-artifacts/launchers/` and
`vm-artifacts/handlers/`, respectively, for graceful-shutdown and domain-exit evidence.

For VM tests, shared client fixture cleanup exports retained Session events, including native frames
in both directions, stderr and lifecycle failures, for
every Sandbox where that client opened a Session. Exports go to
`test.outputs/vm-artifacts/native/<sandbox-uid>/<thread-id>.jsonl`, with archive cursors and origins;
the adjacent JSON records the export watermark. Failed and partially observed turns are included.
