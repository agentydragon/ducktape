# Acceptance against any deployment; `agentplane-testing` retired

Status: **proposed 2026-10-21 UTC; each step starts on the operator's approval.**
Nodes are in the [task DAG](task_dag.md#14-acceptance-on-k3s-and-retiring-agentplane-testing).

The operator's goal (2026-10-21 03:40, 04:11):

- Run the acceptance tests inside k3s instead of on `agentplane-testing`, and get rid of
  `agentplane-testing` entirely.
- Write the scenarios generically, so the same tests point at testing, staging or k3s.
- Allow two kinds of model backend: a scripted mock, and real models. Real models also test that
  Agentplane's instructions are good. Both need not exist at the start.
- Put k3s tests in CI if they get fast enough.

The disposable stack is [`x/k3s_env`](../x/k3s_env/README.md): one k3s per RBE test action, images
built from the tree under test.

## Test tiers

Decided 2026-10-21 06:41 (operator). A test runs in the cheapest tier that has the real thing it
depends on.

| Tier      | What is real                                                                                                                                                       | What it tests                                                                                                                                                |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Unit      | the code, in process                                                                                                                                               | pure logic                                                                                                                                                   |
| Component | one service, PostgreSQL in Docker, an envtest API server ([`kubernetes_apiserver.py`](../testing/kubernetes_apiserver.py) mints Pod-bound tokens), real neighbours | RBAC, watches, admission, token review, a service's API and storage; a neighbour is faked only to inject a fault                                             |
| Harness   | the Go runner, Claude Code and Codex, a scripted model                                                                                                             | harness protocols, Codex code mode included                                                                                                                  |
| System    | the k3s stack with the scripted model                                                                                                                              | every acceptance scenario: what needs a kubelet (running Pods, PVCs, suspend/resume, egress networking, LLM ingress identity) or several services; CI on PRs |
| Live      | a deployment with real models                                                                                                                                      | only where the model's behavior is under test ([which](#two-axes-target-and-model-backend))                                                                  |

The reshuffle this implies:

- **`FAKE_APISERVERS_TO_ENVTEST`.** Hand-written API servers and TokenReview stubs give way to
  envtest.
- **`K3S_VM_ACCEPTANCE`.** VM Sandboxes need KubeVirt on k3s without KVM (finding 3).

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
2. **Python compiles its imports in every action.** Rules_python's precompile
   (`pyc_collection = "include_pyc"`) fails on RBE: the exec interpreter does not find its
   standard library. So each action compiles bytecode as it imports, about three quarters of import
   time. With the fixture chart rendered at build time, imports still take 10–13 s before pytest
   starts ([measurements](../x/k3s_env/README.md#fixture-chart-rendered-at-build-time-2026-10-21)).
3. **What the RBE executor can run** (probed 2026-10-21 with `bb execute`, the k3s target's
   isolation properties): kernel 5.15.0, no `/dev/kvm` (`CONFIG_KVM` unset, only
   `CONFIG_KVM_GUEST`), no BPF JIT. So no KubeVirt with hardware virtualization and no Cilium.
   kube-proxy runs, and the fixture uses ClusterIP Services.

## What `agentplane-testing` covers today

| Scenario                                                                                                         | Needs                                                                                                 | On k3s                                                             |
| ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `test_dispatcher_deployed` (queue, replicas, wake)                                                               | kubectl, Pod-bound tokens, two Gateway replicas                                                       | runs on k3s (#11330)                                               |
| `test_storage_deployed`, Pod case                                                                                | Sandbox-owned PVCs                                                                                    | runs on k3s (#11330)                                               |
| `test_egress`, `test_session_instructions`, `test_launch_presets`, `test_suspend_resume` Pod case                | turns on both harnesses, the egress ring, kubectl                                                     | scripted (no key); the real-model set needs a key                  |
| `test_mcp`, `test_dcr`                                                                                           | Dex operator login, `mcp-everything`, the Dex-backed `example` OAuth server, a browser for Dex's form | runs on k3s with the MCP fixtures                                  |
| VM cases: `test_vm_sandbox`, `test_vm_workspace_ownership`, `test_suspend_resume` VM, `test_storage_deployed` VM | KubeVirt, KVM                                                                                         | no KVM on RBE (finding 3)                                          |
| `test_thread_latency`                                                                                            | browser, Electric, frontend; ceilings for the deployed system                                         | no Electric in the fixture; RBE timings are not deployment latency |
| `test_ollama_routes`                                                                                             | the GPU Ollama                                                                                        | not reachable from RBE                                             |

What testing proves that no test names: Flux reconciling the generated manifests, Cilium policies,
CNPG, External Secrets, cert-manager, Gateway API routes, Kyverno, real image
pins, and a real rollout before staging ([testing before staging](task_dag.md#testing_rollout_role--what-replaces-testing-as-the-stage-before-staging)).

## Two axes: target and model backend

A scenario is written once. It runs against a **target**, a deployment, and a **model backend**,
whatever serves its model turns.

**Targets** (landed in #11330; [acceptance README](../acceptance/README.md)). A scenario declared
with `acceptance_test(k3s = True)` runs against the deployment the environment names
(`AGENTPLANE_ACCEPTANCE_*`, testing by default) and as `<scenario>_k3s`, which starts its own stack
on RBE and publishes it through the same variables. Cases that need VM Sandboxes are marked and the
k3s target deselects them. The Dispatcher and Pod storage scenarios run this way; the rest move as
the steps below give k3s what they need.

**Model backends.**

- **Real model.** This is what the model scenarios use now: the deployment's catalog model.
  - It tests the plumbing, and also that Agentplane's instructions get a model to do the job.
    The real case of `test_session_instructions` works this way: its evidence is the model doing
    what the instruction says.
  - It is non-deterministic and costs money.
  - On k3s it needs a key (`K3S_MODEL_CREDENTIALS`).
- **Scripted model.** The runner tests' `ScriptedModel` ([`scripted_model.py`](../testing/scripted_model.py),
  in both the Responses and Messages dialects), served as LLM ingress's upstream.
  - The test scripts each model step (text, shell call) and asserts what the model was sent: the
    system text, the tool outputs.
  - It is deterministic and needs no key.
  - On k3s the test process runs on the node, so the ingress upstream is the test process at the
    node IP.
  - On testing or staging, the deployed ingress cannot reach a test process. A scripted variant
    there needs a deployed scripted-model endpoint that the test drives, which this plan does not
    schedule.

Most scenarios need only the scripted backend (operator, 2026-10-21 05:38). A real model runs only
where the model's behavior is what is under test:

- `test_session_instructions`: the model obeys the instructions its session was opened with.
- The `test_egress` probe: the model works out from the rules endpoint how to present a credential
  it does not hold.
- Later, a model uses the `agentplane` CLI unprompted.

Everything else is plumbing, and gets a scripted version only. Each plumbing scenario's real version
is replaced by its scripted one in the same PR.

## Test layout

Decided 2026-10-21 05:38 (operator).

- **Scenarios** stay in `agentplane/acceptance/test_<behavior>.py`, one module per behavior. A
  module holds that behavior's version for each backend it has, and the system-side steps and
  checks they share: create the Sandbox, suspend it, read the egress decisions. Most modules have
  a scripted version only.
- **One test function per backend.** A function names its backend with a marker, `real_model` or
  `scripted_model`, and each target's plugin deselects what it cannot serve, as the k3s target
  does for `vm_sandboxes`.
  - A real-model function takes the `model` fixture (the catalog's model for the harness) and
    sends prompts with `Agent.run`. Its evidence is the system's record, plus the answer where
    the behavior is about instructions.
  - A scripted function takes a `scripted` fixture: the `ScriptedModel` for its Sandbox and
    harness. It runs the turn with `Agent.run` and answers each model request with `reply`
    (`Text`, `ShellCall`). Its evidence is the system's record, plus what the harness sent.
  - Example, `test_session_instructions` (renamed from `test_instructions`, operator 2026-10-21
    05:57). The real version checks that the model's answer carries the marker the instructions ask
    for. The scripted version checks that the instructions reach the model's system text, for
    Claude, or its developer instructions, for Codex, and only in their own session.
- **Bazel targets.** `acceptance_test` makes one target for each (target, backend) pair the
  scenario has:

  | Target                                | Backend  | Bazel target                                                              |
  | ------------------------------------- | -------- | ------------------------------------------------------------------------- |
  | Deployed (testing, or staging by env) | real     | `<scenario>`, manual and local, as now                                    |
  | k3s                                   | scripted | `<scenario>_k3s`, on RBE; with the model-free scenarios, the CI candidate |
  | k3s                                   | real     | `<scenario>_k3s_real`, after `K3S_MODEL_CREDENTIALS`                      |
  | Deployed                              | scripted | not scheduled                                                             |

- **The scripted upstream** is [`scripted_upstream.py`](../acceptance/scripted_upstream.py), in place
  of LiteLLM behind LLM ingress on the k3s stack ([acceptance README](../acceptance/README.md)).
- **Targets** stay where #11330 put them: the environment variables in `conftest.py`, the k3s
  plugin in `x/k3s_env/acceptance_target.py`.

## The program

Ordered by what unblocks what. Speed work runs alongside and blocks only the CI tier.

1. **`K3S_MODEL_CREDENTIALS`** (decision), then **`K3S_MODEL_SCENARIOS`**: the real-model set on
   k3s. Until then that set runs against testing or staging.
   - The fixture today takes a Qwen-only key through `--test_env`, which lands on the BuildBuddy
     command line.
   - No BuildBuddy secret for model access exists yet (operator, 2026-10-21 05:22). How one is
     provisioned is part of this decision.
   - Recommended: a Terraform-provisioned, budget-capped LiteLLM key for k3s acceptance, offering
     models on both harnesses. That means ChatGPT luna for Codex, which the operator allowed for
     tests.
   - Deliver the key as a BuildBuddy secret rather than a flag. BuildBuddy RBE secrets are unused
     in this repo so far.
2. **`K3S_VM_ACCEPTANCE`**: where VM-Sandbox acceptance runs with no testing cluster.
   - In flight: a spike of KubeVirt with software emulation (`useEmulation`) inside the RBE k3s, on
     the Bazel-built guest (`K3S_VM_IMAGE`) (operator, 2026-10-21 06:42).
   - If a guest boot under emulation is too slow to be a test, VM acceptance runs against staging
     as an operator-run check: the same scenarios, a different target.
   - Executors with `/dev/kvm` would need self-hosted BuildBuddy executors, which
     [the self-hosting notes](../../docs/buildbuddy_selfhost.md) list as Enterprise-only.
3. **`TESTING_ROLLOUT_ROLE`** (decision). Testing is also the stage that risky rollouts hit before
   staging.
   - Recommended: the k3s acceptance suite on the exact commit gates the staging image pins, and
     staging is the first real cluster.
   - The real-model suite runs against staging after a rollout.
   - Flux, Cilium and CNPG-level changes then land on staging directly, with a rollback plan in
     the PR.
4. **`TESTING_ENVIRONMENT_RETIRE`** (existing node, moved). Three PRs:
   - The recipes, memories and RBAC roles that name testing.
   - The Flux kustomization, generated manifests, DNS and routes.
   - The testing databases and volumes. This is a data drop, so it is its own PR and lands only on
     the operator's word.

   `test_thread_latency` and `test_ollama_routes` stay staging-only manual checks.

Speed, in parallel:

- **Precompiled bytecode**: up to 10 s of the remaining imports (finding 2). Measured here only; no
  node until the operator wants one.
- **Python removal**: 43% of the bytes, 9–14% of the action. The Action Service port is paused
  behind service boundaries, and the app has no port plan. This program schedules neither.
- **Go image bytes**: 313 MiB over eight images that share nothing. Measured here only; no node
  until the operator wants one.

## CI tier

**`K3S_CI_TIER`** (decision, after the scripted backend). What blocks CI:

- **Time on slow workers.** Set the budget on the slowest of 10 fresh runs, not the median.
- **Reliability.** Each admitted target needs 20 consecutive fresh green runs
  (`--runs_per_test=20 --nocache_test_results`) with no flake.
- **Credentials.** Fork PRs never get the model key. Real-model scenarios run only where the key is.
- **Selection.** Every Agentplane image is an input, so bazel-diff selects these targets on most
  Agentplane PRs. Drop the `external` tag so unchanged inputs cache, and keep each target one
  action.

Recommended:

- On PRs: the model-free and scripted-model k3s targets, once they meet the budget.
- `x/k3s_env` leaves `x/` in the PR that admits its first target to CI.
- Post-merge on `devel`: the real-model targets. They judge Agentplane's instructions against a
  model, which is a signal to watch rather than a gate for a code change.
