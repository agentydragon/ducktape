# k3s environment spike

Does the Agentplane testing chart come up on a bare k3s in an RBE action (no Cilium, no
kube-proxy)? The model-free lifecycle target runs in the separate
[`Agentplane k3s lifecycle` workflow](../../../.github/workflows/agentplane-k3s-lifecycle.yml), launched explicitly with
`workflow_dispatch`. It does not run automatically on pull requests or pushes while its cost remains
high. Agents can still run it
directly with `bbr test //agentplane/x/k3s_env:test_lifecycle --test_output=all`. The separate
`test_env` model smoke remains manual and requires `LITELLM_QWEN_KEY`.

Both targets drive the app's REST/SSE API and omit Electric and the browser frontend. Tests of the
frontend sync protocol run Electric separately. The database bootstrap still creates the `electric`
role required by the existing migrations; it does not start an Electric process.

[`fixture_chart.py`](fixture_chart.py) constructs the chart directly
with shared service constructs and explicit bare-k3s settings. It supplies fixture Secrets,
headless Services, local-path storage and a test egress CA. One PostgreSQL 18 Deployment uses the
repo's pinned OCI image, an emptyDir data volume and [`bootstrap.sql`](bootstrap.sql) to create the
service-owned databases. Unsupported Cilium, Gateway API, Kyverno, Prometheus, cert-manager and External
Secrets resources are omitted during construction. The chart instantiates the Go Runner Gateway
directly with one gRPC listener on port 8080; the stable `runner-gateway` and `session-dispatcher`
Service names remain. A Goose init container runs before the Go server container and uses the
Dispatcher database Secret. The
model-free lifecycle and model smoke tests share [`stack.py`](stack.py), which starts k3s, streams
the Bazel-built OCI image layouts into containerd, applies the rendered stack and reports Deployment
readiness.

The image list includes the Go Runner Gateway and Goose queue migration images. A fast
[`test_render`](BUILD.bazel) check validates this fixture composition and its endpoint/Service wiring
without starting Kubernetes. The `test_lifecycle` target exercises the full model-free stack and has
no model credential dependency; the workflow only supplies the BuildBuddy credential needed to run
Bazel remotely. The `test_env` target is the separate live model smoke and is not run by this workflow.
The importer consumes the `oci_image` targets through `oci_layout_rloc`, combines their index
descriptors with the rendered tags, and streams each distinct blob once. It does not materialize
Docker-format `oci_load` tarballs.

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

## Lifecycle measurements

These profiles and outcomes describe the earlier Python Gateway/Dispatcher fixture. They predate
the Go fixture composition above and are not image-size or lifecycle measurements for the Go images.

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
provide real KubeVirt, Cilium, ClusterIP routing or OIDC/Dex coverage, and this target is not
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
- **KubeVirt kinds:** the sandbox controller's cache needs `VirtualMachine`/`VirtualMachineInstance` to exist, so
  schemaless stub CRDs are applied. It should instead start without KubeVirt when the kinds are absent.
- **PostgreSQL:** a single-replica Deployment uses the repository's pinned PostgreSQL 18 image, an emptyDir data
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
  gomplate rejected it. The direct fixture also leaves Dex out.

- **Runner image:** the `runner` SandboxTemplate's image is Nix-built and private. `:runner_image` builds an
  equivalent from the runner plus the pinned Claude Code and Codex binaries at `/bin/claude` and `/bin/codex`
  (debian-slim base, no toolbox), so the environment needs no registry credential. The harness links go in
  `/usr/bin` (the base's `/bin` is a symlink to it) and name the canonical `+http_archive+` repo directories. The test
  creates a `Sandbox` on that template through the app, opens a Claude session on the catalog's Qwen model and
  asserts the turn completes; the reply is kept as the `answer.txt` test output.
- **Storage classes:** the manifest's `local-path-ovh-hdd` does not exist on k3s, so every `storageClassName`
  becomes `local-path`; without it the Sandbox's state PVC never binds and the Pod stays Pending.
- **LiteLLM:** `LITELLM_QWEN_KEY` (the Qwen-only key Terraform provisions as
  `litellm/litellm-key-disposable-env`; pass it with `--test_env=LITELLM_QWEN_KEY=...`) is required. It replaces
  `litellm-key-cheap-experiments`, and llm-ingress and the egress rules dial `https://litellm.allegedly.works`
  instead of the cluster-internal Service.

Not yet reached: Service port≠targetPort under headless Services (egress is 80→8082; only the agent-facing
rules URL uses it) and Dex/OIDC public hostnames. The k3s lifecycle tests use stub KubeVirt CRDs and no
Cilium, so they do not validate those integrations or real external identity.

The [historical image inventory](k3s_image_inventory_2026_10_14.md) records the layer and unpacked-file
breakdown for the 21-image Python-service snapshot before Sandbox Service image retirement and this
Go fixture conversion. Its image sizes are historical and do not measure the Go images now selected
by the fixture.
