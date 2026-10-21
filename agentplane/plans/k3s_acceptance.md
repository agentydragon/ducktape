# Acceptance against any deployment; `agentplane-testing` retired

Status: **approved by the operator 2026-10-21 UTC (tiers 06:41, layout 05:38, PR CI 08:05, VM on k3s 06:42 and 08:31);
two decisions and the steps after them remain.** The tiers are in [test tiers](../docs/test_tiers.md); targets,
backends and layout in the [acceptance README](../acceptance/README.md).
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

## What `agentplane-testing` covers today

| Scenario                                                                                                         | Needs                                                                                                 | On k3s                                                                 |
| ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `test_dispatcher_deployed` (queue, replicas, wake)                                                               | kubectl, Pod-bound tokens, two Gateway replicas                                                       | runs on k3s (#11330)                                                   |
| `test_storage_deployed`, Pod case                                                                                | Sandbox-owned PVCs                                                                                    | runs on k3s (#11330)                                                   |
| `test_egress`, `test_session_instructions`, `test_launch_presets`, `test_suspend_resume` Pod case                | turns on both harnesses, the egress ring, kubectl                                                     | scripted (no key); the real-model set needs a key                      |
| `test_mcp`, `test_dcr`                                                                                           | Dex operator login, `mcp-everything`, the Dex-backed `example` OAuth server, a browser for Dex's form | runs on k3s with the MCP fixtures                                      |
| VM cases: `test_vm_sandbox`, `test_vm_workspace_ownership`, `test_suspend_resume` VM, `test_storage_deployed` VM | KubeVirt, KVM                                                                                         | `_k3s_vm` targets: emulated KubeVirt on the test guest, scripted model |
| `test_thread_latency`                                                                                            | browser, Electric, frontend; ceilings for the deployed system                                         | no Electric in the fixture; RBE timings are not deployment latency     |
| `test_ollama_routes`                                                                                             | the GPU Ollama                                                                                        | not reachable from RBE                                                 |

What testing proves that no test names: Flux reconciling the generated manifests, Cilium policies,
CNPG, External Secrets, cert-manager, Gateway API routes, Kyverno, real image
pins, and a real rollout before staging ([testing before staging](task_dag.md#testing_rollout_role--what-replaces-testing-as-the-stage-before-staging)).

## The program

Ordered by what unblocks what. Speed work runs alongside and shortens PR CI, which runs every
`_k3s` target (operator, 2026-10-21 08:05).

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
   - Recommended: the `_k3s_real` targets run post-merge on `devel`, not on PRs. They judge
     Agentplane's instructions against a model, a signal to watch rather than a gate for a code
     change, and fork PRs never get the key.
2. **`TESTING_ROLLOUT_ROLE`** (decision). Testing is also the stage that risky rollouts hit before
   staging.
   - Recommended: the k3s acceptance suite on the exact commit gates the staging image pins, and
     staging is the first real cluster.
   - The real-model suite runs against staging after a rollout.
   - Flux, Cilium and CNPG-level changes then land on staging directly, with a rollback plan in
     the PR.
3. **`TESTING_ENVIRONMENT_RETIRE`** (existing node, moved). Three PRs:
   - The recipes, memories and RBAC roles that name testing.
   - The Flux kustomization, generated manifests, DNS and routes.
   - The testing databases and volumes. This is a data drop, so it is its own PR and lands only on
     the operator's word.

   `test_thread_latency` and `test_ollama_routes` stay staging-only manual checks.

The real-model `<scenario>_k3s_real` targets come with `K3S_MODEL_SCENARIOS`; a scripted variant against a
deployment is not scheduled, since the deployed ingress cannot reach a test process.

Speed, in parallel:

- **Precompiled bytecode**: up to 10 s of the remaining imports
  ([executor constraints](../x/k3s_env/README.md#rbe-executor-constraints)). Measured here only; no
  node until the operator wants one.
- **Python removal**: 43% of the bytes, 9–14% of the action. The Action Service port is in flight
  (`ACTION_SERVICE_GO_PORT`), and the app has no port plan. This program schedules neither.
- **Go image bytes**: 313 MiB over eight images that share nothing. Measured here only; no node
  until the operator wants one.
