# TODO

## Standards

- Potential indexing (property-specimen cross-refs) if/when scale requires it
- Policy: should verbatim docstring repetition in ABC subclass methods violate no-useless-docs? Lean yes, undecided.
- Property naming mismatch: "self-describing names" vs "use datetime for datetimes". Decide scope or split.
- Target Python version detection/guidance for agents/graders

## Features

- Reimplement `fix` command as critic-driven loop: run critic, fix issues, rerun until clean or max iterations
- Agent timeout warning handler: inject "5 minutes remaining" messages using `created_at` + `timeout_seconds`
- If automated prompt optimization becomes a product priority again, evaluate GEPA or a successor
  against the current definition-based `run_critic()` and critic-dev architecture. The previous
  adapter was retired after the legacy critic runner disappeared and the exposed command stopped working.

## LLM Proxy

- Add a richer Chat Completions transcript renderer if the raw JSON fallback
  becomes painful. Keep the raw fallback even after adding a renderer because
  provider-specific fields such as `reasoning_content` are useful during
  debugging.
- Add a first-class provider/model capability only if we route z.ai through
  Chat Completions again. The live GLM path uses the Anthropic Messages shape
  because z.ai's Chat Completions tool-call parser mishandles union-shaped tool
  parameters; see `props/debug/glm46_strict_tool_schema_experiments.md`.
- Defer a dedicated `correlation_id` column until DB-to-Langfuse joins need it.
  Current correlation rides in injected props metadata.

## Testing

- Anthropic-shape agent e2e test. The agent e2e tests (critic/grader/critic_dev) only
  exercise the OpenAI shapes via `props/testing/fake_openai_server.py`; the Anthropic
  `/v1/messages` path is covered only by `props/agents/af/test_client.py` unit/respx tests.
  This gap let two crash bugs ship to live runs (the `store` kwarg the Anthropic SDK rejects,
  and the `/v1/v1/messages` double-path). Add a fake-anthropic-server (analog of
  `fake_openai_server.py`) and an agent e2e that drives `AnthropicClient → props-llm-proxy
/v1/messages → mock backend` so the anthropic path is covered in CI.

## Infrastructure

- Decide the fate of the agent image push. The `props-agents` job in
  <.github/workflows/push-images.yml> is disabled: it failed on every devel push against the
  down registry. Either props comes back and the job is re-enabled (delete the `false &&`),
  or it is not coming back and the job, `//props/agents:push_images_bin`, and the
  `PROPS_REGISTRY_*` CI secrets go with it. Leaving it disabled indefinitely is the one
  outcome to avoid — a job that never runs stops being maintained but still looks live.

- Sane story for applying migrations without full `db recreate` (direct `alembic upgrade head`)
- Bulk specimen sync in `props db sync` from Bazel bundle artifacts (currently one-by-one via `sync-specimen`)
- GLM per-model pricing. `cluster/k8s/props/app/config.toml` assigns all 7 z.ai GLM models
  glm-4.6's placeholder rates (input 0.39 / output 1.74 per 1M). Set real-ish per-model z.ai
  rates so budget accounting is accurate. Low urgency: z.ai is currently a flat prepaid
  subscription (quota-based), so per-token cost is largely cosmetic for budgeting.

## Native Execution

- Add a real Kubernetes API test harness for agent orchestration. The old kind
  spike on BuildBuddy RBE was blocked because Firecracker workers lacked
  `CONFIG_KEYS`, while OCI workers had the host kernel but no privilege. Viable
  paths: get `CONFIG_KEYS=y` into the Firecracker worker kernel, maintain a
  patched `kindest/node` that skips the keyring sysctls, or use envtest for
  Pod-spec/reconcile logic while keeping Docker E2Es for container execution.
- Namespace-isolate agent Pods from the backend and DB-admin-adjacent services.
  Agents should reach logs through `GET /api/runs/{id}/logs` and the LLM through
  `props-llm-proxy`, not by directly reaching infra endpoints in the shared
  `props` namespace.
- Replace the `grader_definition_changed`/builtin-tag image-roll trigger with a
  Flux image-automation pointer for the current grader digest, then have
  `GraderSupervisor` reconcile grader Pods onto that digest.
