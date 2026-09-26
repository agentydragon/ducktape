# Optional coding smoke eval

This is a small, optional readiness screen for local-model coding. It is not a priority in the
current program, a benchmark, or a SWE-bench claim. It runs two fixed tasks—stable topological
ordering and a Unicode-aware JSONL transform—through Claude Code and Codex, for four cells total.

The current backend reuses Agentplane's deployed client and disposable Sandbox lifecycle. By
default, it requests `llama-cpp/oai-chat/qwen3.8-27b-q8`; that model must be offered by the selected
deployment and harness. This target does not configure LiteLLM, Agentplane, or a local llama.cpp
server. It is manual and runs only on the controlled host with access to the testing deployment.

Each cell seeds a small task repository in its own Sandbox, asks the agent to implement the task and
run the visible unittest suite, then restores and byte-verifies the canonical tests before the
controller runs those tests independently inside the Sandbox. A result requires successful fixed
tests and a nonempty implementation diff; the model's final claim is not used as the score. The
controller records the complete Git status, including untracked and ignored paths, and the diff from
the seed commit alongside the fixed-test output. Generated code and tests execute inside the
Sandbox, not on the host.

## Run one cell

Each turn is bounded at 600 seconds, allowing about 30,000 generated tokens at the observed
50-token-per-second rate. Run one cell per Bazel invocation: the default acceptance token lasts
1800 seconds and can expire during a four-cell matrix. A caller-supplied
`AGENTPLANE_ACCEPTANCE_TOKEN` must remain valid for the entire cell.

```bash
bazelisk test //x/local_llm/eval:test_local_coding \
  --test_env=LOCAL_LLM_CODING_CASE=harness_codex-stable-topological-order \
  --test_output=streamed
```

Select one of `harness_codex-stable-topological-order`, `harness_codex-jsonl-transform`,
`harness_claude-stable-topological-order`, or `harness_claude-jsonl-transform`. Override the
default route with `LOCAL_LLM_CODING_MODEL` when a different model is offered to the selected
harnesses. Evidence files are written under
`bazel-testlogs/x/local_llm/eval/test_local_coding/test.outputs/`.

The acceptance fixtures default to `https://agentplane-testing.allegedly.works` and mint a
short-lived bearer token through the caller's kubeconfig. See the [Agentplane acceptance
instructions](../../../agentplane/acceptance/README.md#controlled-host-preflight) for RBAC,
environment overrides, and the controlled-host preflight. The target uses `manual` and
`no-remote-exec`; CI and RBE do not select it.
