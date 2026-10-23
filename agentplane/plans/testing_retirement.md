# Retiring `agentplane-testing`

Status: **in progress.** The system tests run on k3s in PR CI (tiers approved 2026-10-21 06:41, PR CI 08:05). The
operator's direction (2026-10-21 22:02): move every workflow off the testing instance, give RBE a real-model key
for the tests that need one, and delete the testing instance. The tiers are in [test tiers](../docs/test_tiers.md);
targets and backends in the [system tests README](../system_tests/README.md). Nodes are in the
[task DAG](task_dag.md#14-system-tests-on-k3s-and-retiring-agentplane-testing).

## What uses testing today

| Workflow                                                                                                           | Moves to                                                  | Node                         |
| ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------- | ---------------------------- |
| Risky config rolled out on testing first                                                                           | staging after the system tests pass, with a rollback plan | `STAGING_FIRST_ROLLOUT`      |
| Testing-only pieces: Flux kustomization and image pins, Dex MCP client, Action groups, LiteLLM key, DNS and routes | deleted                                                   | `TESTING_ENVIRONMENT_RETIRE` |

Image pins already roll to staging without waiting on testing; testing gates nothing but the operator's habit of
trying risky config there first (2026-10-12: testing down is better than staging down). No test covers what only a
real cluster exercises (Flux, Cilium, CNPG, External Secrets, cert-manager, Gateway API, Kyverno); once testing is
gone, staging is the first cluster such a change reaches.

## Steps

1. **`STAGING_FIRST_ROLLOUT`**: the docs and recipes that send risky config to testing first say staging, after the
   system tests, with a rollback plan in the PR.
2. **`TESTING_ENVIRONMENT_RETIRE`**, three PRs: the Flux kustomization, image pins, manifests, DNS and routes; the
   Dex MCP client, Action groups, LiteLLM key, `*-acceptance-*` Secrets and RBAC; the testing databases and volumes,
   a data drop the operator cleared for any time (2026-10-23: nothing there is kept). The k3s fixture builds on
   testing's cdk8s environment, so the first PR moves what the fixture reads into it.

Speed, in parallel:

- **Precompiled bytecode**: up to 10 s of the remaining imports
  ([executor constraints](../x/k3s_env/README.md#rbe-executor-constraints)). Measured here only; no
  node until the operator wants one.
- **Python removal**: the measurements above predate the completed Action Service Go port.
  Remeasure before using those percentages to prioritize more ports; the app has no port plan.
- **Go image bytes**: 313 MiB over eight images that share nothing. Measured here only; no node
  until the operator wants one.
