# k3s environment spike

Does the Agentplane testing chart come up on a bare k3s in an RBE action (no Cilium)? The
model-free lifecycle target runs in the separate
[`Agentplane k3s lifecycle` workflow](../../../.github/workflows/agentplane-k3s-lifecycle.yml), launched explicitly with
`workflow_dispatch`. It does not run automatically on pull requests or pushes while its cost remains
high. Agents can still run it
directly with `bbr test //agentplane/x/k3s_env:test_lifecycle --test_output=all`. The separate
`test_env` model smoke remains manual and requires `LITELLM_QWEN_KEY`.

System test scenarios run on the same stack as the k3s targets in
[`agentplane/system_tests`](../../system_tests/README.md), which PR CI runs:
[`system_test_target.py`](system_test_target.py)
is the pytest plugin that starts it and publishes it as the deployment under test, with LLM ingress
forwarding to the scenario's scripted model on the node. Scripted targets take the
`harnesses/runner_layout` image, which carries both pinned harnesses.

containerd keeps unpacked image layers (its overlayfs snapshotter's root) on a tmpfs, except on a stack
with VM Sandboxes ([measurements](#image-layers-in-memory-2026-10-21)).

Each stack records its setup in `k3s-timeline.jsonl` among the test's undeclared outputs: when each
step finished and how long each image took to import, written as it happens, so a hung run shows how far
it got. An acceptance target's test log ends with the same timeline.

A target declared with `mcp_fixtures = True` takes the `rendered/local-model-mcp-fixtures.json`
rendering and also imports and runs Dex, `mcp-everything` and the OAuth MCP fixture
(`mcp_fixtures/*_layout`), with test-only Dex clients, operator, MCP server and Action groups. Dex
reads a config Secret the chart writes, holding the bcrypt hash of a generated operator password.
The app and Dex serve on their in-cluster Service names, which OIDC redirects carry;
[`stack.py`](stack.py) adds the Services' addresses to the node's `/etc/hosts` so the test process
reaches the same names. The app serves http. Dex serves TLS too, because the app and the Action
Service accept a plain-http issuer only on loopback: a CA made per stack signs its certificate, and
the app, the Action Service and the test process trust it through `SSL_CERT_FILE`. The Action
Service's own OAuth server, which `test_dcr` registers with, is such an issuer too, so it names itself
by a loopback origin that `stack.py` port-forwards to the Action Service.

Both targets drive the app's REST/SSE API and omit Electric and the browser frontend. Tests of the
frontend sync protocol run Electric separately. The database bootstrap still creates the `electric`
role required by the existing migrations; it does not start an Electric process.

k3s runs its bundled kube-proxy, so the fixture's Services are ordinary ClusterIP Services and Pods
use cluster DNS and the `kubernetes` Service. `test_lifecycle` checks both from inside the app's Pod.
**Gotcha:** kube-proxy needs the iptables `MARK` target from the executor's guest kernel, which no
image change supplies. The executor lacked it until 2026-10-21 and [#10808](https://github.com/agentydragon/ducktape/pull/10808)
was reverted for that; only a real lifecycle run, never a green `test_render`, shows an executor
can run it.

[`fixture_chart.py`](fixture_chart.py) constructs the chart directly
with shared service constructs and explicit bare-k3s settings. It supplies fixture Secrets,
local-path storage and a test egress CA. The fixture runs the Go egress proxy
and Goose history migrator through the same construct as testing, with fixture-only PostgreSQL TLS
settings. The shared construct supplies the egress RBAC, policies, trust mounts, images and flags.
One PostgreSQL 18 Deployment uses the
pinned published CNPG minimal image, an emptyDir data volume and
[`postgres_entrypoint.sh`](postgres_entrypoint.sh) with [`bootstrap.sql`](bootstrap.sql) to create
the service-owned databases. Unsupported Cilium, Gateway API, Kyverno, Prometheus, cert-manager and External
Secrets resources are omitted during construction. The chart instantiates the Go Runner Gateway
directly with one gRPC listener on port 8080; the stable `runner-gateway` and `session-dispatcher`
Service names remain. A Goose init container runs before the Go server container and uses the
Dispatcher database Secret. The
model-free lifecycle and model smoke tests share [`stack.py`](stack.py), which starts k3s, streams
the Bazel-built OCI image layouts into containerd, applies the rendered stack and reports Deployment
readiness. It uses the pinned K3s binary from Bazel runfiles, so lifecycle actions do not download K3s
at runtime.

Node readiness and the appearance of the bundled DNS/storage Deployments share a 180-second
bootstrap deadline. API probes have bounded subprocess timeouts and stop when k3s exits;
bootstrap failures include the last probe result and k3s log tail and stop the server process.

The image list includes the Go Runner Gateway and Goose queue migration images. A fast
[`test_render`](BUILD.bazel) check validates this fixture composition and its endpoint/Service wiring
without starting Kubernetes. The `test_lifecycle` target exercises the full model-free stack and has
no model credential dependency; the workflow only supplies the BuildBuddy credential needed to run
Bazel remotely. The `test_env` target is the separate live model smoke and is not run by this workflow.
The importer consumes the `oci_image` targets through `oci_layout_rloc`, combines their index
descriptors with the rendered tags, and streams each distinct blob once. It does not materialize
Docker-format `oci_load` tarballs.

The runner Pod is laid out as in production: the real runner comes from the production `//agentplane/runner:disk_image`
image volume at `/run/agentplane/runner`, and the tools image holds the rest. The lifecycle target supplies
`:runner_image`, with no native harnesses. The model smoke supplies `:runner_claude_image`, which adds only
pinned Claude Code to the same base. Each target carries its own `runner_layout.rloc` through
`K3S_RUNNER_LAYOUT_RLOCATION`; the shared stack library carries only service layouts, among them the runner
disk image (`agentplane-runner-disk:unset`). Both variants import as the existing `runner:unset` tag, so
templates, registration and lifecycle behavior stay the same. Image volumes need containerd 2.1 or later
and the `ImageVolume` feature gate, which `stack.py` turns on in the API server and kubelet. This fixture
split does not select a deployment-wide harness-delivery mechanism.

## RBE executor constraints

Probed 2026-10-21 with `bb execute` under the k3s target's isolation properties: kernel 5.15.0, no `/dev/kvm`
(`CONFIG_KVM` unset, only `CONFIG_KVM_GUEST`) and no BPF JIT. So the stack runs no Cilium, KubeVirt only with
software emulation, and kube-proxy with ClusterIP Services.

**Gotcha:** rules_python's precompile (`pyc_collection = "include_pyc"`) fails on RBE, because the exec
interpreter does not find its standard library. Each action compiles bytecode as it imports, about three
quarters of import time; with the chart rendered at build time, imports still take 10–13 s before pytest starts
([measurements](#fixture-chart-rendered-at-build-time-2026-10-21)). Worker speed dominates run-to-run variance
([per-worker timings](#phase-timings-and-image-bytes-2026-10-21)), so a time budget has to hold on slow workers.

## Lifecycle coverage

The model-free [lifecycle tests](test_lifecycle.py) wait for the Sandbox's own `Ready=True`
condition, check the runner process is non-root, check a bootstrap-created file, and
suspend until the effective mode is `Suspended` and the Pod is gone. Resume waits for Ready again
and verifies the state-volume marker survived.
The controller gates Ready on workload readiness, runner connection and bootstrap success.

Exact bootstrap and seal-before-stop ordering belongs in deterministic
[controller tests](../../sandbox_controller/controller_test.go), including
`TestReadyWaitsForTheBootstrapAndStopsOnItsFailure` and
`TestSuspensionWaitsForTheRunnerToQuiesce`. The 2026-10-15 coverage audit found these tests and the
existing eventual k3s outcomes cover the requested lifecycle checks; no extra transition assertions
or expensive rerun were needed. New quiesce behavior still needs its own focused regression tests.

## VM Sandboxes

A system test scenario's `_vm` target ([system tests README](../../system_tests/README.md)) runs on a
stack rendered with VM Sandboxes (`rendered/local-model-vm.json`), so [`vm_sandboxes.py`](vm_sandboxes.py)
installs production's KubeVirt release with `useEmulation` (QEMU TCG; the executor has no `/dev/kvm`)
and Kyverno's admission controller, which adds the egress relay to each launcher Pod as in
production. The fixture applies once KubeVirt's VirtualMachine API is served: the Sandbox Controller
looks for it only at start. The stack writes KubeVirt's and Kyverno's view of the VMs, each guest's
serial console and any image a Pod pulled from a registry to `test.outputs/vm-sandboxes.txt`.

The template boots the [test guest](guest/BUILD.bazel) instead of production's NixOS image, which sits
in a private registry and would boot for minutes without KVM: a Debian cloud kernel that KubeVirt boots
directly, a busybox initramfs that mounts an erofs root under a tmpfs overlay, and an init that gives
the runner what [`vm.nix`](../../runner/vm.nix) does. The runner comes from the production runner disk,
as in a Pod; both harnesses, Git and `findmnt` are pinned in the root disk.

KubeVirt's and Kyverno's images are Bazel-pulled layouts (`vm_sandboxes/`, pinned in `MODULE.bazel`),
imported before their releases are applied, so no Pod pulls them; the releases roll out while the
stack's own images import.

### VM stack timings, 2026-10-21

Booting, suspending and resuming one VM Sandbox with those images imported
([invocation](https://app.buildbuddy.io/invocation/0929e52f-9074-4bf7-8565-7ee4c7720432)) took 350 s
of action time; pulling them at run time, 405 s and 429 s. Temporary markers in `stack.py`, not
committed, timed the stack from k3s's start:

| Seconds after k3s starts                                       |   s |
| -------------------------------------------------------------- | --: |
| k3s up                                                         |  19 |
| KubeVirt's and Kyverno's images imported, 1.15 GB of blobs     |  81 |
| Their releases applied                                         |  89 |
| The stack's own images imported, 1.52 GB                       | 167 |
| Every Deployment Available, KubeVirt and Kyverno already ready | 202 |

Importing the two sets at once took 169 s and 202 s, against 58 s and 78 s one after the other
([invocation](https://app.buildbuddy.io/invocation/a7f1a8e7-b891-4495-9252-a4327412d10b); that worker
was slower throughout, k3s up at 47 s).

The `_vm` targets in one invocation ([invocation](https://app.buildbuddy.io/invocation/b1635c1d-db13-4262-842f-cf07b912d07b)),
each on its own worker: `test_vm_sandbox_vm` (two VM Sandboxes, a turn on each) 483 s,
`test_storage_vm` 492 s, `test_suspend_resume_vm` 597 s; the Pod case,
`test_suspend_resume`, 216 s.

In a run with pulls ([invocation](https://app.buildbuddy.io/invocation/989d9e78-957b-4e0c-8f54-717e5f679e59)),
create, Ready, suspend, resume and Ready again took under 2 minutes; the resumed VM was running 14 s
after its VMI was created. In the run before it, a Sandbox's VM was scheduled 18 s after creation and
running 3 s later; the emulated guest reached `/init` at 6 s, and the runner started about 14 s after
the VM was running.

## Lifecycle measurements

The harness comparison measures the Go Gateway/Dispatcher fixture with shared typed service
constructs, one plain PostgreSQL instance, and a pinned K3s binary. Electric and the browser
frontend remain outside this test. Earlier snapshots below use different source revisions;
only each explicitly paired experiment supports its own comparison.

### Image layers in memory, 2026-10-21

The [containerd import tail](#containerd-import-tail) profile found unpacking bound by filesystem writes.
One run each of `test_thread_stream`, importing the same images one `ctr images import` per image:

| containerd storage                                 | Import, s | Action, s | Invocation                                                                            |
| -------------------------------------------------- | --------: | --------: | ------------------------------------------------------------------------------------- |
| Disk, every image streams all its blobs            |     107.7 |     235.0 | [10eae102](https://app.buildbuddy.io/invocation/10eae102-419a-45f1-8670-3868978cd6af) |
| Whole root on tmpfs, every image streams all blobs |      75.6 |     182.5 | [c78cffa4](https://app.buildbuddy.io/invocation/c78cffa4-7619-4420-945d-bf1c716d4d38) |
| Snapshotter root on tmpfs, shared blobs once       |      64.8 |     190.7 | [d1c5e40b](https://app.buildbuddy.io/invocation/d1c5e40b-d99b-40e4-9d9b-a6f802e69195) |

The first two ran alone; the last ran beside `test_mcp` and `test_suspend_resume_vm`, also with the
snapshotter root on tmpfs. `free -m` when each stack was ready (before any scenario ran), in MiB of the
executor's 9724; shared memory is mostly the layer tmpfs:

| Target                   | Shared | Available |
| ------------------------ | -----: | --------: |
| `test_thread_stream`     |   3042 |      4991 |
| `test_mcp`               |   3520 |      4328 |
| `test_suspend_resume_vm` |   4521 |      2426 |

On the VM stack that is close to the tmpfs default cap (half of memory), and 2.4 GiB is left for guests
that request 1.5 GiB each, so a stack with VM Sandboxes keeps its layers on disk.

### Phase timings and image bytes, 2026-10-21

Pytest time of every passing `test_lifecycle` run BuildBuddy holds, by UTC day (each day mixes
branches, and some runs carried temporary profilers; only the paired sections below isolate a change):

| Day   | Landed that day                                            | Runs | Median s | Range s |
| ----- | ---------------------------------------------------------- | ---: | -------: | ------: |
| 10-14 | OCI-layout streaming                                       |    5 |      723 | 534–957 |
| 10-15 | shared Python interpreter layer                            |   13 |      721 | 336–874 |
| 10-16 | Go Gateway/Dispatcher fixture                              |    3 |      293 | 254–338 |
| 10-17 | harness-free runner, Go egress sidecar, Go LLM ingress     |   17 |      204 | 157–309 |
| 10-21 | Go History and Notifications, k3s 1.34, runner disk volume |    5 |      158 |  98–318 |

Three fresh runs on `c0d2b833` ([invocation](https://app.buildbuddy.io/invocation/703e55fa-95f8-41bc-9cc1-c8b6d9aeb2f1),
`--runs_per_test=3 --nocache_test_results`) passed all four cases. Temporary markers in `stack.py`,
not committed, wrote wall-clock phase times; the RBE execution metadata gives the action start and end:

| Seconds on each worker                              | Fast | Middle | Slow |
| --------------------------------------------------- | ---: | -----: | ---: |
| Before pytest starts                                |   27 |     78 |   41 |
| k3s up, CoreDNS moved to the node                   |   13 |     15 |   25 |
| Image import: streaming the blobs to containerd     |   21 |     30 |   54 |
| Image import: containerd finishing after the stream |   13 |     30 |   35 |
| CRDs and manifests applied                          |    3 |      5 |    7 |
| All Deployments Available                           |   31 |     35 |   53 |
| The four lifecycle cases                            |   40 |     40 |   42 |
| Teardown and upload                                 |    7 |      6 |   12 |
| Action execution                                    |  156 |    241 |  269 |

PostgreSQL started in about 2 s in every run; each service with a migration init container failed
its first connection and waited one 10 s `CrashLoopBackOff` before starting. In the slower
[ffa9564c](https://app.buildbuddy.io/invocation/ffa9564c-3c9d-4937-b091-9fc8b191104d) run (318 s
pytest, 421 s action, 586 CPU-seconds) the back-offs reached 40 s; in
[a973af66](https://app.buildbuddy.io/invocation/a973af66-e28b-48b9-8ae2-c1111feeb441) the same
fixture took 98 s pytest, 131 s action and 166 CPU-seconds.

The import is 84 distinct blobs, 938,776,320 bytes. Stored layer bytes by image group, from the
build outputs of the same revision (PostgreSQL from its pinned descriptor sizes):

| Images                                                                                                          |   MiB |
| --------------------------------------------------------------------------------------------------------------- | ----: |
| Python: app, app migrate, Action Service, Action migrate                                                        | 410.7 |
| Go: Gateway, egress, Notifications, Sandbox Controller, History, Policy Controller, LLM ingress, egress sidecar | 313.0 |
| Goose migrators (4)                                                                                             |  24.9 |
| Runner disk image and the tools image's own layers                                                              |  68.0 |
| PostgreSQL                                                                                                      |  78.8 |

382.1 MiB of the Python group is used by no other image; the rest is the Debian base the tools
image shares. The eight Go images share under 1 MiB.

### Fixture chart rendered at build time, 2026-10-21

The test module imported the cdk8s chart code, and with it jsii and Node, only to render the
manifest; rendering itself took about 1 s. `render_fixture` now renders one manifest per model
backend at build time, and `rendered_fixture.load` fills in the CAs and model address.
Three fresh `test_lifecycle` runs each, with temporary wall-clock markers
([before](https://app.buildbuddy.io/invocation/0f6663f7-3cca-458b-b7f2-0cbfdb095df7),
[after](https://app.buildbuddy.io/invocation/94bb75ae-3b07-4f5e-8955-ba3788f2ebaf)):

| Seconds per run                               | Before           | After            |
| --------------------------------------------- | ---------------- | ---------------- |
| Test module imports                           | 27.0, 42.8, 28.2 | 12.1, 12.6, 10.1 |
| Action start to `pytest_bazel.main()`         | 36.2, 52.6, 37.7 | 22.8, 23.5, 15.8 |
| Manifest ready in the fixture (render / load) | 1.5, 1.3, 0.9    | 0.1, 0.1, 0.1    |

What is left before pytest is 5–10 s of VM and workspace setup and the remaining imports, mostly
bytecode compilation ([executor constraints](#rbe-executor-constraints)).

The test process no longer builds the app's `Settings`, which parses argv, so pytest flags such as
`--test_arg=-s` work on `test_lifecycle` and the system test k3s targets
([run](https://app.buildbuddy.io/invocation/93bfc317-4354-46fd-b19b-fac04650fb20)). `test_render`
still renders the chart in process and rejects them.

### Migrators retrying their first connection, 2026-10-21

The Goose and Alembic migrators retry their first PostgreSQL connection in process
([`migrate`](../../postgres/go/migrate/migrate.go), [`db_migrations`](../../../util/db_migrations.py)).
Three fresh lifecycle runs each, on `devel` and with the change, with temporary uncommitted markers
in `stack.py`:

| Three fresh runs                              | Before ([0f6663f7](https://app.buildbuddy.io/invocation/0f6663f7-3cca-458b-b7f2-0cbfdb095df7)) | After ([6c7624c6](https://app.buildbuddy.io/invocation/6c7624c6-272c-4006-b012-250c6ab0cbbd)) |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------: | --------------------------------------------------------------------------------------------: |
| Migration init container restarts, per run    |                                                                                      10, 10, 9 |                                                                                       0, 0, 0 |
| Manifests applied to Deployments Available, s |                                                                               61.8, 51.6, 36.2 |                                                                              39.7, 36.1, 30.8 |
| Last migrator done after PostgreSQL Ready, s  |                                                                                     14, 17, 15 |                                                                                       2, 3, 3 |

What is left of that phase is mostly the services' readiness probes: a 3 s initial delay and a
10 s period, so a service that is not ready at its first probe waits 10 s more.

### Published PostgreSQL minimal image, 2026-10-17

The fixture uses `ghcr.io/cloudnative-pg/postgresql:18.6-minimal-trixie`, pinned for linux/amd64 at
`sha256:e5fffed938c8b55b41d9a6a88a47424d46f90aa15d807aa60348b85ea5bd498f`.
[CNPG minimal images](https://github.com/cloudnative-pg/postgres-containers#minimal-images) omit LLVM
JIT starting with PostgreSQL 18. They expect operator-managed startup, so this fixture supplies a
20-line Bash entrypoint: initialize as UID 26, bootstrap existing service roles/databases over a
Unix socket with TCP disabled, then start PostgreSQL with SCRAM authentication for network clients.
Logical WAL and the existing connection limit remain. Initialization explicitly uses `C.UTF-8`;
this test does not establish locale-specific sorting equivalence with the previous image.
The shared Testcontainers PostgreSQL image is unchanged.

A [fresh compatibility run](https://app.buildbuddy.io/invocation/bb29d0db-2a6f-4a38-88cc-b7b197d15268)
passed all four unchanged lifecycle cases (create, bootstrap, suspend/resume with retained state,
and delete), the fixture render test, and changed database-library mypy/ruff checks. Lifecycle
TestRunner execution took 208.385 seconds. This is one unprofiled run without a matched timing
control; no test speedup is established. [Recorded evidence](cnpg-minimal-compatibility-2026-10-17.json)
retains the source/patch provenance, execution worker, per-case timings and test XML hash.

The published image has 82,606,366 stored layer bytes (78.78 MiB), versus 162,433,831 (154.91 MiB)
for the prior image in the [captured import inventory](import-blob-inventory-2026-10-17.md).
Its different Debian base does not share a layer digest with the other captured images, so the
net saving across that fixture's unique layers is 50,034,807 bytes (47.72 MiB). These are stored
layer sizes, not unpacked storage or measured import-time savings.

### Go LLM ingress comparison, 2026-10-17

Two fresh lifecycle actions per variant used source `b67c382141da29f1474f3468c87fb5839b26a0ed`.
The Go variant used that source unchanged. The Python control restored only `agentplane/llm_ingress/`
from before #10848 (`5b3d9a1d2cda2b36650a64d7d7930c1503512bcc`); the Go Gateway, shared workload
authentication and relay remained unchanged. Both ran
`bbr test --nocache_test_results --runs_per_test=2 --test_output=errors //agentplane/x/k3s_env:test_lifecycle`.
Assertions, timeouts and the lifecycle test source were unchanged. All four actions passed all four cases.

| Mean of two runs (seconds)                                      | Python ingress | Go ingress |
| --------------------------------------------------------------- | -------------: | ---------: |
| TestRunner execution                                            |        218.700 |    228.266 |
| Create and wait for Sandbox Ready, including shared stack setup |        146.747 |    156.843 |
| Bootstrap check                                                 |          8.320 |      8.346 |
| Suspend and resume                                              |         14.650 |     14.670 |
| Delete                                                          |         12.945 |     11.903 |

The action ranges overlap: Python **207.279–230.122 s**, Go **219.744–236.788 s**. Both variants
used the same two workers, with opposite directions per worker: Python/Go were 207.279/236.788 s
on `5ac56964-24d3-492a-9ea0-d1af3ecf75d1` and 230.122/219.744 s on
`1e09c634-7833-4073-8376-aee72b0db4fc`. Go ran first, then Python, without alternating variants.

**No lifecycle speedup is established.** Go averaged 9.565 s (4.4%) slower in this sample, but two
runs per variant and uncontrolled order/cache/scheduling effects do not establish a regression.
This model-free test measures stack startup and Sandbox lifecycle, not LLM throughput or model latency.
The earlier post-relay Go mean was 232.966 s; the current 228.266 s is 2.0% lower, but that historical
comparison includes other source/environment changes and cannot isolate the ingress rewrite.

Execution metadata and test XML are in the [Go invocation](https://app.buildbuddy.io/invocation/4f62d23a-3847-4a2d-8f86-78114fb5f0e2)
and [Python control invocation](https://app.buildbuddy.io/invocation/503ae967-96c4-4d1a-b64e-d7cd170d0f46).
[Raw measurements](llm-ingress-go-comparison-2026-10-17.json) retain per-case timings, workers,
source and the control patch digest. The control can be reproduced by restoring `agentplane/llm_ingress/` from the parent of #10848
on the stated base. These unprofiled runs do not separate import/unpack from service startup.

### Native transfer versus local import, 2026-10-17

Two fresh profiled lifecycle actions per mode used source
`1290a748c5d1275b95b69ee354383413987dada4`, the same temporary cProfile wrapper, and the same
lifecycle cases, assertions and timeouts. `K3S_IMPORT_MODE=local` temporarily added only `--local`
to `ctr images import`; the transfer mode used the existing command. No `--no-unpack` or custom
import scheduler was used. The instrumentation patch SHA-256 is
`67ec43e87956b57bad2f07d266de37cc2a7ff7b844c3a4fe8883bb97ea9e3620`; instrumentation is not retained.

[Transfer-mode results](https://app.buildbuddy.io/invocation/a96dd4cc-aa24-4d86-9e19-ec51aa1f6837)
and [local-mode results](https://app.buildbuddy.io/invocation/b216fbed-50a4-421f-b9f0-fd042b37f2bd)
retain test XML, cProfile data and `import-inputs.json` for each run.
[Extracted measurements](native-import-comparison-2026-10-17.json) retain the source, workers,
per-case results and common input-manifest hash. All four manifests match after excluding the mode:
18 image tags, 91 distinct OCI blobs and 910,772,050 blob bytes. This includes manifests/configs,
not just layer payloads; it is not unpacked storage usage.

| Mean of two runs (seconds)                                | Transfer | `--local` |
| --------------------------------------------------------- | -------: | --------: |
| TestRunner execution                                      |  230.299 |   232.834 |
| `K3s.import_images`                                       |   79.427 |    83.198 |
| `K3s.start`                                               |   20.155 |    19.711 |
| `running_stack` setup and teardown                        |  148.720 |   151.619 |
| Create and wait for Sandbox Ready, including shared setup |  157.459 |   160.664 |
| Suspend and resume                                        |   14.750 |    15.328 |
| Delete                                                    |   13.081 |    13.979 |

Every action passed all four cases. The same two workers ran both variants:

| Worker                                 | Transfer action (s) | Local action (s) |
| -------------------------------------- | ------------------: | ---------------: |
| `5ac56964-24d3-492a-9ea0-d1af3ecf75d1` |             227.421 |          235.354 |
| `1e09c634-7833-4073-8376-aee72b0db4fc` |             233.176 |          230.315 |

**Retain transfer mode.** `--local` averaged 2.536 s (1.1%) slower overall and 3.771 s (4.7%)
slower in import, with overlapping ranges and opposite per-worker directions. This provides no evidence of a
useful speedup and does not justify a default change. Two samples per mode, transfer first and
local second, cannot establish a small regression or steady-state effect. Both variants were
profiled, and import/setup/Ready rows overlap; do not add them or compare these totals as an
uninstrumented baseline. End-to-end Ready and lifecycle outcomes include any unpack work deferred
past import. Failed DNS attempts before dispatch are excluded from these execution timings.

### Imported OCI blobs, 2026-10-17

The [complete blob inventory](import-blob-inventory-2026-10-17.md) maps every blob to its images
and contents, using the exact transfer-mode TestRunner inputs above. All 91 digests and sizes
match the captured import manifest: 55 filesystem layers (910,712,261 bytes), 18 manifests and
18 configs (59,789 bytes), totaling 868.58 MiB stored. Layer downloads were SHA-256 verified;
this is the lifecycle fixture without the optional Claude runner variant.
[Machine-readable evidence](import-blob-inventory-2026-10-17.json) retains the ordered layers,
full digests, media types, build history and input-artifact hash.

All 18 images share the 28.41 MiB Debian base. The 11 Python images share the 202.48 MiB
interpreter layer, but their package layers are distinct because dependency files live under
binary-specific runfiles paths. Each also has an application/runfiles layer and a 10 KiB
interpreter-alias tar; app additionally carries 29.57 MiB of precompiled Python bytecode.
The inventory records the six Go executable layers and PostgreSQL's 12 additional layers too.

Import streams the OCI blobs into containerd's content store, then unpacking applies filesystem
layers into snapshots. Container creation assembles the image snapshots with a writable layer;
it does not copy the full image again. Stored blob sizes are not unpacked storage or filesystem
write totals, and this inventory does not assign per-blob timing. Sharing Python package layers is not
pursued: more services are moving to Go. The native profile still identifies filesystem apply/writes as substantial work.

### Startup profile, 2026-10-17

A fresh [profiled lifecycle run](https://app.buildbuddy.io/invocation/22cdbd11-6ee9-4bbf-b25c-6b811e3e4021)
passed all four cases on source `c8f0df8c77b2b843308e1f9855f07e8562111c31`, after the Go directory
collapse. A temporary cProfile wrapper around `pytest_bazel.main()` and its output-helper dependency
were removed before publication; the instrumentation patch SHA-256 is
`14250f41980de275234be16317bfb4ef9f643088802b03aa7ebd03ccc60867fa`.
The invocation retains `lifecycle.prof`, `lifecycle-pstats.txt`, `k3s.log` and test XML;
[extracted measurements](startup-profile-2026-10-17.json) retain the source, worker and timings.

| Measurement                                                 | Seconds |
| ----------------------------------------------------------- | ------: |
| TestRunner execution                                        | 243.448 |
| cProfile total                                              | 210.493 |
| `running_stack` setup and teardown                          | 167.849 |
| `K3s.start`                                                 |  18.642 |
| `K3s.import_images`                                         | 101.475 |
| ↳ `tarfile.add`                                             |  30.881 |
| ↳ importer process exit wait after streaming                |  70.561 |
| `running_stack` calls to kubectl for apply and availability |  43.686 |

These are inclusive function times: the import subrows are inside `import_images`, and startup,
import, apply/availability and teardown are inside `running_stack`. Do not sum every row. The
profile begins inside the test entry point; the action additionally includes launcher/venv setup,
imports before the profiler, and profile-output work.

Image import is the largest measured fixture phase: **41.7% of action time**. Most import time
is the **70.6 s wait after the full tar stream is sent**; tar streaming takes 30.9 s and includes
pipe backpressure. This does not identify containerd's CPU work or distinguish decompression,
content ingestion and snapshot unpacking. The 43.7 s kubectl aggregate includes resource application
and Deployment availability checks, not only process initialization; it does not assign a startup
cost to each service.

#### Containerd import tail

A [follow-up diagnostic run](https://app.buildbuddy.io/invocation/5604804f-51fd-4abf-ac66-dc579abcf8ff)
on `44298d8645d25ecb8d3914d35b8608f0c2ef55f9` passed all four lifecycle cases in 243.614 s.
Temporary configuration enabled a Unix debug socket in the then-pinned containerd `v2.0.5-k3s2`.
Immediately after closing the tar input stream, concurrent requests collected a 45-second CPU
profile, a 10-second Go execution trace and goroutine stacks. The invocation retains the raw
profiles and `ctr-import-help.txt`; [extracted measurements](containerd-tail-profile-2026-10-17.json)
include artifact hashes, instrumentation hash and analysis commands. No instrumentation or debug
socket configuration remains in the test. Waiting for capture completion can extend the import
phase, so this diagnostic action is not a new uninstrumented timing sample.

| Containerd CPU profile, 45.18 s window | CPU-seconds | Share of samples |
| -------------------------------------- | ----------: | ---------------: |
| Total sampled CPU                      |      132.11 |             100% |
| Unpacker → filesystem apply            |      114.08 |           86.35% |
| `os.File.Write`, cumulative            |       89.55 |           67.78% |
| SHA-256 block hashing, flat            |        6.25 |            4.73% |

The cumulative rows overlap: writes occur inside filesystem apply. The captured tail is doing
parallel layer extraction, averaging about 2.9 containerd CPU cores during the sample. This identifies
substantial filesystem-write work in the unpacker; it does not establish that more concurrency helps.
The pinned `ctr images import` defaults to the transfer API (`--local=false`), consistent with the
profile's `localTransferService.importStream` and `Unpacker.unpack` stacks.

The 10-second trace also contains aggregate syscall delays of 65.02 s in writes, 10.67 s in
`openat` and 2.15 s in `fdatasync`. Its 154.81 s in child-process waits comes from
`archive/compression.cmdStream`, so decompressor children are active too. These are summed across
parallel goroutines, not additional wall seconds or independent phases. The parent CPU profile
excludes those child processes; their waits can include decompression and pipe backpressure from
extraction, so they do not prove decompression is the limiting CPU cost.

**Unpack limit:** containerd `v2.2.7-k3s1` (k3s v1.34.12, the current pin) has the transfer plugin option
`max_concurrent_unpacks`, but only the [pull path](https://github.com/k3s-io/containerd/blob/v2.2.7-k3s1/core/transfer/local/pull.go)
passes it to the unpacker. The [import path](https://github.com/k3s-io/containerd/blob/v2.2.7-k3s1/core/transfer/local/import.go),
which `ctr images import` takes unless given `--local`, builds the unpacker without a limiter, so no
configuration bounds this archive-import unpack work. On the earlier `v2.0.5-k3s2` the option did not exist.
The native `--local` comparison above (on `v2.0.5-k3s2`) found no useful speedup; retain the transfer pipeline.

### Harness-specific runner comparison, 2026-10-16

Base source: `3fba7794f4278bc66d41d9ceb11dd2ee08656ac3`, including the Service-networking revert.
Two fresh lifecycle actions per variant passed all four existing cases, with unchanged assertions,
timeouts and startup behavior. The Claude-only image also passed the
[real Qwen smoke](https://app.buildbuddy.io/invocation/626648ee-f346-4b93-902a-1f5b81eda432)
at implementation revision `ac82cfd883b3db120f23c4816efb41d2e7e0de30` (one model test, 266.68 s
in pytest). The smoke validates the narrower image; it is not a matched model-latency comparison.

The manual workflow ran the same `--nocache_test_results --runs_per_test=2` command in
[baseline](https://app.buildbuddy.io/invocation/9f7b5851-71e2-4c62-b60e-a18334a90dee) and
[harness-free](https://app.buildbuddy.io/invocation/63f952b3-aec8-4b50-a3fa-4effb82a9fc1)
invocations. Each test action uses `recycle-runner=false` and starts a fresh k3s/containerd.
This forces test execution, not cold Bazel image builds, uploads or worker CAS caches.

| Measurement (seconds)              | Baseline run 1 | Baseline run 2 | Harness-free run 1 | Harness-free run 2 |
| ---------------------------------- | -------------: | -------------: | -----------------: | -----------------: |
| TestRunner execution               |        496.666 |        290.347 |            244.868 |            267.635 |
| cProfile total                     |        435.616 |        248.781 |            208.053 |            226.988 |
| `K3s.import_images` cumulative     |        254.760 |         94.821 |             87.739 |            109.470 |
| `K3s.start` cumulative             |         53.448 |         16.721 |             18.736 |             19.091 |
| `running_stack` setup and teardown |        373.829 |        174.834 |            159.307 |            180.034 |
| Action input-fetch wall time       |          4.050 |          5.089 |              1.029 |              1.837 |

Mean TestRunner execution fell from **393.506 s to 256.251 s (34.9%)** in this sample. Both
harness-free runs were faster than both baseline runs. Adopt the image split: it removes unused
payload and preserves the real lifecycle and Claude coverage.

This is two samples per variant on four different workers, not a steady-state saving estimate.
The slower baseline also spent much longer in k3s startup, which precedes runner-image import;
its full slowdown cannot be attributed to harness bytes. Import times overlap across variants,
so this does not establish a consistent import speedup in isolation. Input-fetch measurements
are separate from execution and reflect worker cache state; executor-reported downloaded bytes
also varied between identical baseline inputs. The outer Bazel invocations took 590.386 s and
360.073 s respectively; these include analysis and other actions and are not test execution time.
The expensive workflow remains manual-only.

The identical temporary `cProfile` wrapper is in benchmark baseline
`6be9f0afd0e4ca0a261563de6eb92700a1160198` and candidate
`834596309be051694a1e78967c1f9ce0d41d57a7`; the candidate additionally changes image selection.
The profiler-only patch SHA-256 is
`cc5fcec68449bcab4047704ec6fbb686a981335fa665543641528a5403a9909d`.
The implementation omits profiling and workflow changes. [Profile summaries](profiles-2026-10-16-harness-comparison.txt)
and [performance records](perf-2026-10-16-harness-comparison.json) retain every run, raw-profile
digest/URI, passing pytest XML counts, worker identity, action digest and executor timing/IO data.
The raw `lifecycle.prof` files remain in each invocation's test outputs:

```bash
bbapi artifact download 9f7b5851-71e2-4c62-b60e-a18334a90dee lifecycle.prof --all -o baseline-profiles/
bbapi artifact download 63f952b3-aec8-4b50-a3fa-4effb82a9fc1 lifecycle.prof --all -o harness-free-profiles/
```

### Go egress sidecar comparison, 2026-10-16

The Python control uses current `devel` source `d558dcc30da97d6ddb2ad2f08767161ab545dc0a`.
The Go candidate uses this PR's implementation; the second lifecycle run used exact PR head
`45536854045afc6c6331f32360e9afd7ca4b5465`. The earlier candidate run used
`56a73f58b3e478a0479d48cc6ce50cda30c72eaf`; the Go runtime and image inputs are identical at
both revisions. Each action ran the unchanged four model-free lifecycle cases with fresh test
execution and the same assertions and timeouts. The first test creates a Sandbox and waits for its
`Ready=True` condition. The suspend/resume case also waits for Ready after resume, but its time
includes both transitions and is not an isolated resume measurement.

| Measurement (seconds)             | Python 1 | Python 2 |    Go 1 |    Go 2 |
| --------------------------------- | -------: | -------: | ------: | ------: |
| TestRunner execution              |  356.763 |  329.950 | 231.510 | 234.422 |
| Pytest suite                      |  309.061 |  278.370 | 188.388 | 193.645 |
| Create and wait for Sandbox Ready |  269.361 |  232.487 | 149.621 | 158.002 |
| Bootstrap check                   |   10.547 |   10.772 |   8.627 |   8.417 |
| Suspend and resume                |   14.837 |   19.861 |  15.030 |  12.765 |
| Delete                            |   14.055 |   14.954 |  14.833 |  14.179 |

Mean TestRunner execution fell from **343.356 s to 232.966 s (32.2%)**. The create-and-Ready case
fell from **250.924 s to 153.812 s (38.7%)**; suspend/resume fell from 17.349 s to 13.898 s
(19.9%). All four runs used the same BuildBuddy TestRunner worker ID
`e371cdec-b7b4-40bb-b594-8ac328c689b3`. These are observed lifecycle totals, not a sidecar-only
startup benchmark; controller scheduling, workload readiness, image import and bootstrap are also
included.

One temporary cProfile run per source measured import and K3s startup separately. The same profiler
patch (SHA-256 `cc5fcec68449bcab4047704ec6fbb686a981335fa665543641528a5403a9909d`) changed only the
test entry point and is not part of this PR. Its measurements are single-run samples:

| Profile measurement (seconds)      | Python control | Go sidecar |
| ---------------------------------- | -------------: | ---------: |
| TestRunner execution               |        340.359 |    241.836 |
| Pytest suite                       |        280.224 |    204.030 |
| cProfile total                     |        282.101 |    206.050 |
| `K3s.import_images` cumulative     |        152.042 |     84.142 |
| `K3s.start` cumulative             |         29.303 |     21.706 |
| `running_stack` setup and teardown |        232.419 |    159.082 |

Both profiled runs passed all four cases on the same worker ID as the lifecycle measurements.
`import_images` includes streaming, pipe backpressure and containerd import/unpack work. Its
single-run reduction does not isolate the sidecar's contribution. The fresh lifecycle actions and
their test XML are available in the [Python control 1](https://app.buildbuddy.io/invocation/c151b124-fd5f-480a-950d-5876b7de8a78),
[Python control 2](https://app.buildbuddy.io/invocation/a552cc26-ad92-44e6-b332-e18517d58673),
[Go run 1](https://app.buildbuddy.io/invocation/2ba37e23-5fd7-484a-b860-7b676fc6702b), and
[exact-head Go run 2](https://app.buildbuddy.io/invocation/0a2d7b92-8cb0-4e2c-92cd-cf03c5be7ff3).
The profiled artifacts are in the [Python control](https://app.buildbuddy.io/invocation/2f056e16-9f21-4452-b2e6-56fa9a6330b3)
and [Go sidecar](https://app.buildbuddy.io/invocation/04e2e695-cbb4-4908-81ed-7e6387c884a4)
invocations. Exact-head [Bazel CI](https://github.com/agentydragon/ducktape/actions/runs/38995319654),
[pre-commit](https://github.com/agentydragon/ducktape/actions/runs/38995319700), and
[build artifacts and imports](https://github.com/agentydragon/ducktape/actions/runs/38995319712)
passed on `45536854045afc6c6331f32360e9afd7ca4b5465`.

The actual lifecycle TestRunner input trees contain 17 OCI images. CAS metadata was checked against
SHA-256 and descriptor sizes; image-layer payloads were not fetched again. The sidecar image shrank
from 255,582,262 bytes (243.74 MiB) to 42,070,418 bytes (40.12 MiB), but most of that difference is
the Python runtime layer still used by other images. Across the complete fixture stack, distinct
layer bytes fell from 888,218,462 to 879,900,181 bytes: **8,318,281 bytes (7.93 MiB) removed**.
The shared 212,320,824-byte (202.48 MiB) Python runtime remains in other service images. The
sidecar's exclusive layer bytes fell only from 13,468,780 to 12,277,760 bytes (1.14 MiB); the
rest of the net stack reduction is in other image layers. Do not report the sidecar's full image
size as removed bytes. Raw inventories record every image and layer digest for the [Python control](image-sizes-2026-10-16-egress-sidecar-python.json)
and [exact-head Go action](image-sizes-2026-10-16-egress-sidecar-go.json).

The [real Qwen/Claude model smoke](https://app.buildbuddy.io/invocation/2e0a2213-a09d-48fa-ab47-95f3fa70c63b)
passed on rebased relay revision `b2b339d243`: one fresh test, 268.7 s TestRunner execution,
with `answer.txt` containing `pong`. This is end-to-end functional evidence, not a matched latency comparison.

`import_images` includes tar streaming, pipe backpressure and containerd import/unpack work;
it is not Python CPU time. `running_stack` measures its executing setup/teardown generator,
excluding time paused at its yield. Profile totals start inside the Python entry point;
TestRunner execution additionally includes launcher/venv setup and imports before profiling.

#### Image sizes for the comparison

These are stored OCI layer bytes from the actual action input trees, retaining each layer's
declared compression. The removed harness layer is an uncompressed tar. Metadata was downloaded
and checked by CAS SHA-256 and size; every layer descriptor size matched its input-file size.
Layer payloads were not downloaded again. These are not unpacked filesystem or snapshot sizes.
All 17 non-runner image manifests match across baseline, lifecycle and model-smoke inventories.

| Runner variant | Runner layer bytes | Runner MiB | Distinct layer bytes across stack | Distinct bytes removed |
| -------------- | -----------------: | ---------: | --------------------------------: | ---------------------: |
| Both harnesses |        866,872,179 |      826.7 |                     1,512,840,164 |                      0 |
| No harnesses   |        261,237,619 |      249.1 |                       907,205,604 |            605,634,560 |
| Claude only    |        475,632,499 |      453.6 |                     1,121,600,484 |            391,239,680 |

The lifecycle action removes **577.6 MiB** of distinct layers; the model smoke removes
**373.1 MiB**. Raw inventories retain every image, manifest and layer digest, media type and size:
[baseline](image-sizes-2026-10-16-harness-baseline.json),
[harness-free](image-sizes-2026-10-16-harness-free.json), and
[Claude-only smoke](image-sizes-2026-10-16-claude-only.json).
This measures removing unused harnesses. It does not measure fetching a needed harness at runtime
or choose between a separately mounted artifact and a digest-verified download.

### Earlier Go fixture profile, 2026-10-16

The model-free lifecycle suite passed all four tests on RBE at source revision
`585b95435357252be2e5af4a5327e58c1805b8a5`. A temporary `cProfile` wrapper around
`pytest_bazel.main()` wrote `lifecycle.prof` and a pstats summary to undeclared test outputs. The
instrumentation-only delta was SHA-256
`e76552aad56e45fb142edb37db2bc187d096b7177582c35591520a494df142db`; it was removed after this
run and is not part of this change.

Command: `bbr --invocation-id-file=/tmp/k3s-go-profile.invocation test --test_output=all --nocache_test_results //agentplane/x/k3s_env:test_lifecycle`.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/c0dcc3d3-223e-4954-b1a3-5d1bcd4ad0cd)
records the successful uncached test action and publishes `test.log`, `test.xml`, `k3s.log`,
`lifecycle.prof`, and `lifecycle_profile.txt` as test outputs.

| Measurement                        |    Result |
| ---------------------------------- | --------: |
| BuildBuddy invocation elapsed      | 442.216 s |
| Bazel test target                  |   368.5 s |
| Pytest session                     | 293.006 s |
| cProfile total                     | 294.819 s |
| `K3s.import_images` cumulative     | 148.406 s |
| `K3s.start` cumulative             |  20.923 s |
| `K3s.kubectl` cumulative, 50 calls |  70.889 s |

`import_images` is the dominant phase isolated by this profile. Within it, `tarfile.add` accounts
for 73.064 s of cumulative streaming time, which includes pipe I/O and backpressure and is not CPU
packing cost. The `Popen.wait` call accounts for 75.300 s after the input stream reaches EOF; this is
only the post-stream tail, not total containerd import/unpack time, which overlaps streaming.
`K3s.start` includes k3s startup and node readiness. The aggregate `kubectl` time includes setup,
Deployment waits, and later test operations, so Deployment readiness was not separately isolated.
`running_stack` accumulated 233.106 s while its generator executed setup and teardown; it excludes
time paused at the yielded fixture and is not wall-clock fixture lifetime.

This is one profiled run. The older Python fixture timings below do not provide a matched baseline
for this Go fixture.

#### Image sizes from the profiled action

The table measures the 18 OCI layouts supplied to that same test action, including PostgreSQL.
Sizes were recovered from its CAS input tree and OCI manifests, without rebuilding the images or
rerunning the suite. The TestRunner action digest is
`c2672d54d147f11d5219920d2730131257efa1d41134c74285658310f407156a/505`.
[The raw inventory](image-sizes-2026-10-16.json) records every manifest and layer digest, descriptor
size and media type. Downloaded metadata was checked against its CAS SHA-256 and size; each
layer's descriptor size matched its input-file size. Layer payloads were not downloaded again.

Stored layer bytes exclude OCI config/manifest/index metadata and count shared layers in each
image. Layers retain their declared compression: the runner's harness archive is an uncompressed
tar. These are not unpacked filesystem or containerd snapshot sizes. One MiB is 1,048,576 bytes.

| Image                          | Stored layer bytes |   MiB |
| ------------------------------ | -----------------: | ----: |
| `runner`                       |        866,872,179 | 826.7 |
| `app`                          |        331,697,280 | 316.3 |
| `action_service`               |        299,189,894 | 285.3 |
| `egress`                       |        288,691,960 | 275.3 |
| `notification_service`         |        280,119,816 | 267.1 |
| `action_service_migrate`       |        273,539,075 | 260.9 |
| `history_service`              |        266,256,133 | 253.9 |
| `egress_migrate`               |        264,205,213 | 252.0 |
| `app_migrate`                  |        259,611,622 | 247.6 |
| `llm_ingress`                  |        257,731,230 | 245.8 |
| `egress_sidecar`               |        255,582,262 | 243.7 |
| `history_migrate`              |        255,424,764 | 243.6 |
| `notification_service_migrate` |        255,421,864 | 243.6 |
| `postgres`                     |        162,433,831 | 154.9 |
| `sandbox_controller`           |         76,558,738 |  73.0 |
| `runner_gateway_go`            |         74,869,138 |  71.4 |
| `policy_controller`            |         63,072,658 |  60.2 |
| `runner_gateway_queue_migrate` |         36,346,258 |  34.7 |

The per-image sum is **4,567,623,915 bytes (4.25 GiB)**. Deduplicating layer digests gives
**1,512,613,866 bytes (1.41 GiB)**, matching the importer's distinct-blob strategy before adding
metadata and tar framing. Thirteen Python images share the **212,320,824-byte (202.5 MiB)**
interpreter layer. The runner's Claude/Codex archive alone is **605,624,320 bytes (577.6 MiB)**.

### OCI-layout streaming, 2026-10-14

Both runs used temporary cProfile instrumentation and passed all four model-free lifecycle tests.
The instrumentation was removed after measurement.

| Measurement (2026-10-14) | Per-image archive import | Combined OCI-layout stream |
| ------------------------ | -----------------------: | -------------------------: |
| Pytest total             |                 869.98 s |                   704.32 s |
| Pytest setup             |                 812.96 s |                   652.23 s |
| cProfile image import    |                 528.43 s |                   444.97 s |

Baseline: `4fcc98579e` plus the profiler patch,
[BuildBuddy run](https://app.buildbuddy.io/invocation/75cf36f3-50c9-423c-bc24-522e00782390).
OCI stream: `ff38137b3f` plus the same instrumentation,
[BuildBuddy run](https://app.buildbuddy.io/invocation/3b656910-9b6f-4b6c-8a7a-e7129375a1fd).
Each run publishes `test.log`, `test.xml`, `k3s.log` and `lifecycle.prof` as test artifacts.

The second run was 165.66 seconds (19%) shorter overall; image import was 83.45 seconds (16%) shorter.
These were individual shared-RBE runs, not a controlled benchmark or an estimate of steady-state CI latency.
This comparison predates the shared interpreter-layer prototype below.

### Shared interpreter layer, 2026-10-15

Both runs passed the same four lifecycle tests with the same temporary cProfile wrapper. The baseline
uses effective source tree `26755c97d4` plus that wrapper. The prototype uses the same tree, the
shared-runtime implementation in this PR, and the identical wrapper. BuildBuddy records different
hosted checkout SHAs (`25eec1a4ff` and `2e3a416799`) because each run applied its retained workspace
delta; the local benchmark worktree preserves the effective base and implementation diff. The
`test_lifecycle.py` input digest (`1e969333`, 5,164 bytes) and test executable digest (`b83e8eba`,
12,129 bytes) match across both invocations, confirming the lifecycle harness itself did not change.
The profiler wrapper is temporary and is not part of this PR.

| Measurement       |   Baseline | Shared runtime | Change |
| ----------------- | ---------: | -------------: | -----: |
| Bazel test target | 1007.585 s |      601.612 s | −40.3% |
| Pytest session    |   956.92 s |       559.30 s | −41.6% |
| cProfile total    |  959.340 s |      563.184 s | −41.3% |
| `running_stack`   |  891.638 s |      507.908 s | −43.0% |
| `import_images`   |  578.430 s |      220.195 s | −61.9% |

The [baseline invocation](https://app.buildbuddy.io/invocation/f23c3f8b-b66d-407e-9c4e-f36216228d4e)
and [prototype invocation](https://app.buildbuddy.io/invocation/f880b028-836b-477b-9bae-ac325ef255e1)
ran their TestRunner actions on the same RBE worker (`f05088d0-ba91-49d8-b520-b21739b84c12`). This is
one matched pair, not a multi-run estimate: it supports an observed improvement for this fixture, but
does not establish steady-state CI savings. The profile shows image import fell by 358.235 seconds;
readiness was not timed as a separate phase, so this does not claim a readiness improvement.

Image import still takes 220.195 seconds in this run. The next performance work should profile its
remaining OCI/containerd import and unpack cost, while keeping lifecycle assertions and timeouts
unchanged. Do not use this one pair to justify moving the expensive workflow into general CI.

The separate workflow runs only the model-free lifecycle target. Its bare-cluster fixtures do not
provide Cilium or OIDC/Dex coverage (KubeVirt runs only in the system tests' `_vm` targets), and this target is not
Dispatcher multi-replica acceptance. Keep the `agentplane-testing` environment and fixture/spike
cleanup as separate future work until those coverage gaps have an explicit owner and replacement.

Earlier evidence from [#10414](https://github.com/agentydragon/ducktape/pull/10414) was 784 seconds for
four lifecycle tests and 590 seconds for the model test; those timings predate Dispatcher deployment.

## Historical result: Python Gateway and Dispatcher fixture

Before the Go fixture rewrite, every Deployment except Dex (13, the app included) became Available,
and a pod Sandbox started and ran a real Qwen turn through the app on one RBE action (about 10 minutes
with the images already in the cache). This is historical Python-service fixture evidence, not a
result for the new Go composition. Breaks found on the way, in order:

- **Kernel sysctls:** the kubelet needs every `/proc/sys/kernel` entry present in the shadow copy, not only
  the keys ones.
- **Disk:** the original 16-image spike inventory was 3.4 GiB compressed across 46 layers, with Python
  interpreters duplicated under each binary's `.runfiles` tree. The shared-runtime prototype moves those
  interpreter files into one layer reused by the Python images; containerd still needs about 1 GiB per image.
  The default 12 GiB disk failed on the 10th image; `EstimatedFreeDiskBytes=50GB` fixes it.
- **No ClusterIP:** without kube-proxy, Pods reach neither the `kube-dns` Service nor `kubernetes.default`.
  CoreDNS runs with `hostNetwork` on the node IP, the kubelet's `cluster-dns` points at it, and every
  container gets `KUBERNETES_SERVICE_HOST`/`PORT` for the node IP.
- **KubeVirt:** an earlier fixture applied schemaless VM/VMI CRDs to satisfy the controller cache.
  The Sandbox Controller now discovers whether those APIs are served, so the current fixture starts
  without them: Pod Sandboxes work and VM Sandboxes report `KubeVirtMissing`.
- **PostgreSQL:** a single-replica Deployment uses the pinned published CNPG minimal PostgreSQL 18 image, an emptyDir data
  volume, and a headless `postgres-primary` Service that works without kube-proxy. `bootstrap.sql` creates each
  service role and database with the matching owner; the separate `fixture_admin` account exists only to bootstrap
  the disposable database. Logical WAL remains enabled for the existing publication migrations. No operator,
  CRD, release download, or CoreDNS rewrite is needed.
- **No resource limits:** the fixture removes container `resources.limits` (requests stay). The app's
  500m CPU limit throttled Python imports by about 14s each on the shared VM.
- **Pod egress:** flannel `host-gw` does not translate Pod traffic, so the test adds a `MASQUERADE` rule for the Pod
  CIDR using the `iptables` k3s ships; without it Pods resolve `litellm.allegedly.works` but cannot connect.
- **Namespaces:** the manifest's Roles live in `litellm` and `ollama`, so empty ones are created.
- **Dex dropped:** its config reached the Pod through an ExternalSecret template (`regexReplaceAll`, `htpasswd`
  are External Secrets functions). The old YAML rewrite could not evaluate that template, and the image's
  gomplate rejected it. The direct fixture writes Dex's config Secret itself, for targets with `mcp_fixtures`.

- **Runner image:** the fixture builds the runner from source on a debian-slim base without the
  deployed Nix toolbox or private registry credentials. The model smoke adds the pinned Claude
  binary at `/bin/claude`; its link lives in `/usr/bin` because the base's `/bin` is a symlink there.
  The test opens a Claude Session on the catalog's Qwen model and requires a completed turn; the
  reply is retained as `answer.txt`. Lifecycle tests use the harness-free variant.
- **Storage classes:** the manifest's `local-path-ovh-hdd` does not exist on k3s, so every `storageClassName`
  becomes `local-path`; without it the Sandbox's state PVC never binds and the Pod stays Pending.
- **LiteLLM:** `LITELLM_QWEN_KEY` (the Qwen-only key Terraform provisions as
  `litellm/litellm-key-disposable-env`; pass it with `--test_env=LITELLM_QWEN_KEY=...`) is required. It replaces
  `litellm-key-cheap-experiments`, and llm-ingress and the egress rules dial `https://litellm.allegedly.works`
  instead of the cluster-internal Service.

Not yet reached: Dex/OIDC public hostnames. The k3s lifecycle tests use stub KubeVirt CRDs and no
Cilium, so they do not validate those integrations or real external identity.

The [historical image inventory](k3s_image_inventory_2026_10_14.md) records the layer and unpacked-file
breakdown for the 21-image Python-service snapshot before Sandbox Service image retirement and this
Go fixture conversion. Its image sizes are historical and do not measure the Go images now selected
by the fixture.
