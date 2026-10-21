# Acceptance on k3s; `agentplane-testing` retired

Status: **proposal for operator review, 2026-10-21 UTC. No implementation starts before approval.**
Nodes are in the [task DAG](task_dag.md#14-acceptance-on-k3s-and-retiring-agentplane-testing).

The operator's goal (2026-10-21 03:40): run the acceptance tests inside k3s instead of on
`agentplane-testing`, get rid of `agentplane-testing` entirely, and put k3s tests in CI if they get
fast enough. The disposable stack is [`x/k3s_env`](../x/k3s_env/README.md): one k3s per RBE test
action, images built from the tree under test.

## Where the k3s tests are

Measured 2026-10-21 ([phase timings and image bytes](../x/k3s_env/README.md#phase-timings-and-image-bytes-2026-10-21)):

- `test_lifecycle` pytest medians by day fell from 723 s (10-14) to 204 s (10-17) and 158 s
  (10-21). Three fresh runs on `devel` took 156, 241 and 269 s of RBE action time.
- Image import (895 MiB, 84 blobs) is 34–88 s, 22–33% of the action. k3s comes up in 13–25 s,
  the Deployments are Available 31–53 s after apply, the four lifecycle cases take 40 s, and 27–78 s
  pass before pytest starts.
- 382 of the 895 MiB belong only to the four Python images (43%). Import is write-bound
  in containerd's unpacker ([profile](../x/k3s_env/README.md#containerd-import-tail)), so removing
  them should save about 15–38 s of import on these workers, plus the app's and Action Service's
  slower start: 9–14% of the action, not most of it.
- `test_env` (one Qwen turn on the same stack) last passed 2026-10-17 at 240–267 s pytest and has
  not run since the History and Notification ports.

### What the Go rewrites bought

- **Egress sidecar** (2026-10-16, paired): action 343 → 233 s (−32%). It runs in every Sandbox
  Pod, so its start was on the critical path.
- **LLM ingress** (2026-10-17, paired): no measurable change.
- **History and Notifications** (2026-10-18 to 10-21, not paired): import bytes went from 869 to
  895 MiB, the new 68 MiB runner disk image included. A Python service shared the 202 MiB
  interpreter and added 17–85 MiB of its own; its Go replacement is 7–55 MiB and shares nothing.
  The 10-21 runs (median 158 s) are within the 10-17 spread.

The larger wins so far were not rewrites: streaming OCI layouts (−19%), the shared interpreter
layer (−40%) and the harness-free runner image (−35%).

## Findings that shape the plan

1. **Worker speed dominates the variance.** On 2026-10-21 the same fixture took 98 s and 318 s
   pytest on different workers (131 s against 421 s of action, 166 against 586 CPU-seconds). Any
   CI budget has to hold on the slow workers, not the median.
2. **Migration crash-loop back-off.** Every service with a migration init container fails its
   first connection while PostgreSQL starts and waits a 10 s back-off; on a slow worker the
   back-offs reached 40 s, a minute of the run.
3. **27–78 s pass before pytest starts.** The test module imports the cdk8s chart code (jsii
   starts Node and loads the libraries) before `pytest_bazel.main()`; `test_render`'s
   [startup note](../x/k3s_env/debug/render_startup.md) found that startup at 29–61 s on RBE. Not
   broken down further here.
4. **What the RBE executor can run** (probed 2026-10-21 with `bb execute`, the k3s target's
   isolation properties): kernel 5.15.0, no `/dev/kvm` (`CONFIG_KVM` unset, only
   `CONFIG_KVM_GUEST`), no BPF JIT. So no KubeVirt with hardware virtualization and no Cilium.
   `MARK` and `-m mark` rules now insert and delete, which is what kube-proxy lacked on 2026-10-16
   ([networking note](../debug/2026-10-16-k3s-service-networking.md)).
5. **`--test_arg=-s` breaks these targets:** the app's `Settings`, which the chart code uses, has
   `cli_parse_args=True` and rejects the test's own argv.

## What `agentplane-testing` covers today

| Scenario                                                                                                         | Needs                                                                                                 | On k3s                                                             |
| ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `test_dispatcher_deployed` (queue, replicas, wake)                                                               | kubectl, Pod-bound tokens, two Gateway replicas                                                       | portable, no model                                                 |
| `test_storage_deployed`, Pod case                                                                                | Sandbox-owned PVCs                                                                                    | portable; drop the testing-quota precondition                      |
| `test_egress`, `test_instructions`, `test_launch_presets`, `test_suspend_resume` Pod case                        | turns on both harnesses, the egress ring, kubectl                                                     | portable once a model key reaches the action                       |
| `test_mcp`, `test_dcr`                                                                                           | Dex operator login, `mcp-everything`, the Dex-backed `example` OAuth server, a browser for Dex's form | needs Dex and both MCP fixtures in the chart                       |
| VM cases: `test_vm_sandbox`, `test_vm_workspace_ownership`, `test_suspend_resume` VM, `test_storage_deployed` VM | KubeVirt, KVM                                                                                         | no KVM on RBE (finding 4)                                          |
| `test_thread_latency`                                                                                            | browser, Electric, frontend; ceilings for the deployed system                                         | no Electric in the fixture; RBE timings are not deployment latency |
| `test_ollama_routes`                                                                                             | the GPU Ollama                                                                                        | not reachable from RBE                                             |

What testing proves that no test names: Flux reconciling the generated manifests, Cilium policies,
ClusterIP Services, CNPG, External Secrets, cert-manager, Gateway API routes, Kyverno, real image
pins, and a real rollout before staging ([testing before staging](task_dag.md#testing_rollout_role--what-replaces-testing-as-the-stage-before-staging)).

## The program

Ordered by what unblocks what. Speed work runs alongside and blocks only the CI tier.

1. **`K3S_ACCEPTANCE_STACK`.** The acceptance `conftest` gets the k3s stack as a target: a
   session-scoped fixture starts `running_stack` and yields its URL, token, namespace and
   kubeconfig in place of the testing defaults. A macro makes one `<scenario>_k3s` target per
   scenario file, each its own RBE action, so files run in parallel. Model-free scenarios first:
   Dispatcher queue/replicas/wake and the Pod storage case.
2. **`K3S_MODEL_CREDENTIALS`** (decision), then the model scenarios. The fixture today takes a
   Qwen-only key through `--test_env`, which lands on the BuildBuddy command line. Recommended: a
   Terraform-provisioned, budget-capped LiteLLM key for k3s acceptance, offered models on both
   harnesses (ChatGPT luna for Codex, as the operator allowed for tests), delivered as a BuildBuddy
   secret rather than a flag (BuildBuddy RBE secrets are unused in this repo so far).
3. **`K3S_FIXTURE_IDENTITY`.** Dex with a plain config Secret (the ExternalSecret template is what
   dropped it from the fixture), `mcp-everything`, the `example` OAuth server, and an operator-login
   kubeconfig guard that accepts the fixture's apiserver. Then `test_mcp` and `test_dcr` on k3s.
4. **`K3S_VM_ACCEPTANCE`** (decision). Where VM-Sandbox acceptance runs with no testing cluster.
   Recommended: spike KubeVirt with software emulation inside the RBE k3s on the Bazel-built guest
   (`K3S_VM_IMAGE`); if a guest boot under emulation is too slow to be a test, VM acceptance moves
   to staging as an operator-run check. Executors with `/dev/kvm` would need self-hosted
   BuildBuddy executors, which [the self-hosting notes](../../docs/buildbuddy_selfhost.md) list as
   Enterprise-only.
5. **`TESTING_ROLLOUT_ROLE`** (decision). Testing is also the stage risky rollouts hit before
   staging. Recommended: the k3s acceptance suite on the exact commit gates the staging image
   pins, and staging is the first real cluster; Flux/Cilium/CNPG-level changes then land on
   staging directly, with a rollback plan in the PR.
6. **`TESTING_ENVIRONMENT_RETIRE`** (existing node, moved). Three PRs: recipes, memories and RBAC
   roles that name testing; the Flux kustomization, generated manifests, DNS and routes; the
   testing databases and volumes (a data drop, so its own PR and only on the operator's word).
   `test_thread_latency` and `test_ollama_routes` stay staging-only manual checks.

Speed, in parallel:

- **`K3S_KUBE_PROXY_RETEST`**: rerun the reverted #10808 on today's executor (finding 4). If
  ClusterIP works, the fixture drops headless Services, node-IP API access and host-network CoreDNS,
  and gains the Service routing it does not cover now.
- **`K3S_TEST_STARTUP`**: profile the 27–78 s before pytest starts (finding 3) and cut it, for
  example by rendering the fixture chart at build time with the runtime values filled in by the test.
- **`K3S_MIGRATOR_CONNECT_RETRY`**: the migrators retry their first database connection in process
  for a bounded time instead of exiting into `CrashLoopBackOff` (finding 2): 10 s on a typical run,
  about a minute on a slow one. A retry, not a start gate, and no ordering in the test harness.
- **Python removal**: 43% of the bytes, 9–14% of the action. The Action Service port is paused
  behind service boundaries and the app has no port plan; this program does not schedule either.
- **Go image bytes** (313 MiB over eight images that share nothing) is measured here only; no
  node until the operator wants one.

## CI tier

**`K3S_CI_TIER`** (decision, after steps 1 and 2). What blocks CI:

- **Time on slow workers.** Set the budget on the slowest of 10 fresh runs, not the median.
- **Reliability.** 20 consecutive fresh green runs (`--runs_per_test=20 --nocache_test_results`)
  with no flake, per target admitted.
- **Credentials.** Fork PRs never get the model key; model scenarios run only where the key is.
- **Selection.** Every Agentplane image is an input, so bazel-diff selects these targets on most
  Agentplane PRs. Drop the `external` tag so unchanged inputs cache; keep each target one action.

Recommended: the model-free lifecycle and Dispatcher targets on PRs once they meet the budget;
model scenarios post-merge on `devel` until they meet it too.
