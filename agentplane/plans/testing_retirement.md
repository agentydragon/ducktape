# Retiring `agentplane-testing`

Status: **in progress.** The system tests run on k3s in PR CI (tiers approved 2026-10-21 06:41, PR CI 08:05). The
operator's direction (2026-10-21 22:02): move every workflow off the testing instance, give RBE a real-model key
for the tests that need one, and delete the testing instance. The tiers are in [test tiers](../docs/test_tiers.md);
targets and backends in the [system tests README](../system_tests/README.md). Nodes are in the
[task DAG](task_dag.md#14-system-tests-on-k3s-and-retiring-agentplane-testing).

## What uses testing today

| Workflow                                                                                                                   | Moves to                                                  | Node                          |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | ----------------------------- |
| `_live` targets k3s already covers: `test_dispatcher`, `test_storage`, `test_thread_stream`, `test_vm_workspace_ownership` | deleted                                                   | `LIVE_TARGETS_OFF_TESTING`    |
| Live-only checks: `test_thread_latency`, `test_ollama_routes`                                                              | staging                                                   | `LIVE_TARGETS_OFF_TESTING`    |
| Risky config rolled out on testing first                                                                                   | staging after the system tests pass, with a rollback plan | `STAGING_FIRST_ROLLOUT`       |
| Agents' testing operator Role, testing egress policy and the recipes that use them                                         | system tests through `bbr`; staging reads                 | `AGENT_TESTING_ACCESS_RETIRE` |
| Testing-only pieces: Flux kustomization and image pins, Dex MCP client, Action groups, LiteLLM key, DNS and routes         | deleted                                                   | `TESTING_ENVIRONMENT_RETIRE`  |

Image pins already roll to staging without waiting on testing; testing gates nothing but the operator's habit of
trying risky config there first (2026-10-12: testing down is better than staging down). No test covers what only a
real cluster exercises (Flux, Cilium, CNPG, External Secrets, cert-manager, Gateway API, Kyverno); once testing is
gone, staging is the first cluster such a change reaches.

## Steps

1. **`LIVE_TARGETS_OFF_TESTING`**, any time: delete the `_live` targets k3s covers and the `live` variant; point
   `test_thread_latency` and `test_ollama_routes` at staging.
2. **`STAGING_FIRST_ROLLOUT`**: the docs and recipes that send risky config to testing first say staging, after the
   system tests, with a rollback plan in the PR.
3. **`AGENT_TESTING_ACCESS_RETIRE`**: the managed presets stop selecting `agentplane-testing-operator` and the testing
   egress policy; the recipes that used them point at `bbr` system tests and staging reads.
4. **`TESTING_ENVIRONMENT_RETIRE`**, three PRs: the Flux kustomization, image pins, manifests, DNS and routes; the
   Dex MCP client, Action groups, LiteLLM key, `*-acceptance-*` Secrets and RBAC; the testing databases and volumes,
   a data drop that lands only on the operator's word.

Speed, in parallel:

- **Precompiled bytecode**: up to 10 s of the remaining imports
  ([executor constraints](../x/k3s_env/README.md#rbe-executor-constraints)). Measured here only; no
  node until the operator wants one.
- **Python removal**: the measurements above predate the completed Action Service Go port.
  Remeasure before using those percentages to prioritize more ports; the app has no port plan.
- **Go image bytes**: 313 MiB over eight images that share nothing. Measured here only; no node
  until the operator wants one.
