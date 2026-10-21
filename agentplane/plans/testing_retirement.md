# Retiring `agentplane-testing`

Status: **in progress.** The system tests run on k3s in PR CI (tiers approved 2026-10-21 06:41, PR CI 08:05). The
operator's direction (2026-10-21 22:02): move every workflow off the testing instance, give RBE a real-model key
for the tests that need one, and delete the testing instance. The tiers are in [test tiers](../docs/test_tiers.md);
targets and backends in the [system tests README](../system_tests/README.md). Nodes are in the
[task DAG](task_dag.md#14-system-tests-on-k3s-and-retiring-agentplane-testing).

## What uses testing today

| Workflow                                                                                                                   | Moves to                                                  | Node                          |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | ----------------------------- |
| Real-model cases: the `test_egress` probe, `test_session_instructions`' real-model case (`_live`)                          | `<scenario>_real_model` on k3s with the RBE key           | `K3S_MODEL_SCENARIOS`         |
| `_live` targets k3s already covers: `test_dispatcher`, `test_storage`, `test_thread_stream`, `test_vm_workspace_ownership` | deleted                                                   | `LIVE_TARGETS_OFF_TESTING`    |
| Live-only checks: `test_thread_latency`, `test_ollama_routes`                                                              | staging                                                   | `LIVE_TARGETS_OFF_TESTING`    |
| Risky config rolled out on testing first                                                                                   | staging after the system tests pass, with a rollback plan | `STAGING_FIRST_ROLLOUT`       |
| Agents' testing operator Role, testing egress policy and the recipes that use them                                         | system tests through `bbr`; staging reads                 | `AGENT_TESTING_ACCESS_RETIRE` |
| Testing-only pieces: Flux kustomization and image pins, Dex MCP client, Action groups, LiteLLM key, DNS and routes         | deleted                                                   | `TESTING_ENVIRONMENT_RETIRE`  |

Image pins already roll to staging without waiting on testing; testing gates nothing but the operator's habit of
trying risky config there first (2026-10-12: testing down is better than staging down). No test covers what only a
real cluster exercises (Flux, Cilium, CNPG, External Secrets, cert-manager, Gateway API, Kyverno); once testing is
gone, staging is the first cluster such a change reaches.

## The model key on RBE

Resolves `K3S_MODEL_CREDENTIALS`. Today the spike's Qwen-only key reaches `x/k3s_env`'s `test_env` through
`--test_env`, which BuildBuddy records on the command line.

- Terraform mints a LiteLLM key `system-tests-real-model` in [`litellm-keys`](../../tf/gitops/litellm-keys/main.tf):
  the `cheap_experiments_models` lane (Haiku for Claude Code, luna for Codex), `max_budget = 50` over
  `budget_duration = "30d"`, and the usual Kubernetes Secret.
- The operator copies it once into a BuildBuddy org secret, `AGENTPLANE_SYSTEM_TEST_MODEL_KEY`. BuildBuddy secrets
  are set only in the org settings UI.
- `system_test(real_model = True)` generates `<scenario>_real_model`, a k3s target whose `exec_properties` add
  `"test.env-secrets": "AGENTPLANE_SYSTEM_TEST_MODEL_KEY"`. BuildBuddy decrypts that one secret into that test
  action only; no other action, command line or log sees it. The fixture hands it to LLM ingress as its upstream
  Secret, so the Sandboxes never hold it.
- Fork PRs get no BuildBuddy key, so they never run these targets.

Open: when the `_real_model` targets run. They judge Agentplane's instructions against a model, so they are a
signal to watch rather than a gate on a code change. Recommended: after merge on `devel`.

## Steps

1. **`K3S_MODEL_CREDENTIALS`**: the key, the secret and the `real_model` macro option, once the operator picks when
   the targets run.
2. **`K3S_MODEL_SCENARIOS`**: `_real_model` targets for `test_egress` and `test_session_instructions` pass on both
   harnesses; their `_live` targets go. The egress probe binds a fixture-only credential PolicySet. Then
   `K3S_SPIKE_TARGETS_RETIRE` deletes `test_env`.
3. **`LIVE_TARGETS_OFF_TESTING`**, any time: delete the `_live` targets k3s covers; point `test_thread_latency` and
   `test_ollama_routes` at staging. Whichever of steps 2 and 3 lands second deletes the `live` variant.
4. **`STAGING_FIRST_ROLLOUT`**: the docs and recipes that send risky config to testing first say staging, after the
   system tests, with a rollback plan in the PR.
5. **`AGENT_TESTING_ACCESS_RETIRE`**: the managed presets stop selecting `agentplane-testing-operator` and the testing
   egress policy; the recipes that used them point at `bbr` system tests and staging reads.
6. **`TESTING_ENVIRONMENT_RETIRE`**, three PRs: the Flux kustomization, image pins, manifests, DNS and routes; the
   Dex MCP client, Action groups, LiteLLM key, `*-acceptance-*` Secrets and RBAC; the testing databases and volumes,
   a data drop that lands only on the operator's word.

Speed, in parallel:

- **Precompiled bytecode**: up to 10 s of the remaining imports
  ([executor constraints](../x/k3s_env/README.md#rbe-executor-constraints)). Measured here only; no
  node until the operator wants one.
- **Python removal**: 43% of the bytes, 9–14% of the action. The Action Service port is in flight
  (`ACTION_SERVICE_GO_PORT`), and the app has no port plan. This program schedules neither.
- **Go image bytes**: 313 MiB over eight images that share nothing. Measured here only; no node
  until the operator wants one.
