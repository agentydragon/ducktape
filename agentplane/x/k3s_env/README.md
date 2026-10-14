# k3s environment spike

Does the rendered `agentplane-testing` stack come up on a bare k3s in an RBE action (no Cilium, no
kube-proxy)? Manual only: `bbr test //agentplane/x/k3s_env:test_env --test_output=all`.

`render.py` rewrites the generated manifest for a bare cluster: drops Cilium, Gateway API, Kyverno,
Prometheus, cert-manager and External Secrets kinds, emulates the ExternalSecrets with random
Secrets, keeps the CNPG `Cluster` and `Database` objects (the real operator is installed from its release
manifest), makes Services headless (no kube-proxy means no ClusterIP) and generates the egress CA. `test_env.py` starts k3s (recipe from
`agentplane/x/k3s_rbe_spike`), streams the Bazel-built OCI image layouts into containerd, applies everything and reports
per-Deployment readiness.

The image list includes the Session Dispatcher and its migration image, matching the generated
stack. Both tests remain manual; the model-free lifecycle target is the first candidate for CI.
The importer consumes the `oci_image` targets through `oci_layout_rloc`, combines their index
descriptors with the rendered tags, and streams each distinct blob once. It does not materialize
Docker-format `oci_load` tarballs.

## Lifecycle measurements

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
These are individual runs on shared RBE hosts, not a controlled benchmark or an estimate of steady-state
CI latency. Image import still takes about 7.4 minutes and dominates setup. Inspect its containerd
import/unpack work before changing test timeouts; the remaining setup cost includes deployment readiness.
The next CI step remains the model-free target, with the bare-cluster coverage gaps below explicit.

Earlier evidence from [#10414](https://github.com/agentydragon/ducktape/pull/10414) was 784 seconds for
four lifecycle tests and 590 seconds for the model test; those timings predate Dispatcher deployment.

## Result

Every Deployment of the stack except Dex (13, the app included) becomes Available and a pod Sandbox starts and runs a real Qwen turn through the app on one RBE action (about 10 minutes with the images
already in the cache), with the changes below. Breaks found on the way, in order:

- **Kernel sysctls:** the kubelet needs every `/proc/sys/kernel` entry present in the shadow copy, not only
  the keys ones.
- **Disk:** the 16 Bazel-built service images are 3.4 GiB compressed across 46 layers, almost none shared
  (`py_image_layer` puts the interpreter under each binary's own `.runfiles` tree), and containerd needs about
  1 GiB per image. The default 12 GiB disk fails on the 10th image; `EstimatedFreeDiskBytes=50GB` fixes it.
- **No ClusterIP:** without kube-proxy, Pods reach neither the `kube-dns` Service nor `kubernetes.default`.
  CoreDNS runs with `hostNetwork` on the node IP, the kubelet's `cluster-dns` points at it, and every
  container gets `KUBERNETES_SERVICE_HOST`/`PORT` for the node IP.
- **KubeVirt kinds:** the sandbox controller's cache needs `VirtualMachine`/`VirtualMachineInstance` to exist, so
  schemaless stub CRDs are applied. It should instead start without KubeVirt when the kinds are absent.
- **Real CNPG:** the operator runs unmodified (its webhooks are reachable because the apiserver can call Services
  by Pod IP; `failurePolicy` is set to `Ignore`). It generates `postgres-app` itself. Its `postgres-rw` Service is
  a reserved ClusterIP that cannot be disabled, so the Cluster adds a headless `postgres-primary` Service and
  CoreDNS rewrites `postgres-rw.<ns>` to it. The operator and instance Pods also need the `KUBERNETES_SERVICE_*` env.
- **No resource limits:** every container and the CNPG Cluster lose `resources.limits` (requests stay). The app's
  500m CPU limit throttled Python imports by about 14s each on the shared VM.
- **Pod egress:** flannel `host-gw` does not translate Pod traffic, so the test adds a `MASQUERADE` rule for the Pod
  CIDR using the `iptables` k3s ships; without it Pods resolve `litellm.allegedly.works` but cannot connect.
- **Namespaces:** the manifest's Roles live in `litellm` and `ollama`, so empty ones are created.
- **Dex dropped:** its config reaches the Pod through an ExternalSecret template (`regexReplaceAll`, `htpasswd`
  are External Secrets functions). `render.py` does not evaluate those, and the image's gomplate rejects them.
  Dex returns once the emulation renders that template (it needs a bcrypt hash).

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
rules URL uses it) and Dex/OIDC public hostnames.
