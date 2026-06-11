# TODO

## Standards

- Potential indexing (property-specimen cross-refs) if/when scale requires it
- Policy: should verbatim docstring repetition in ABC subclass methods violate no-useless-docs? Lean yes, undecided.
- Property naming mismatch: "self-describing names" vs "use datetime for datetimes". Decide scope or split.
- Target Python version detection/guidance for agents/graders

## Features

- Reimplement `fix` command as critic-driven loop: run critic, fix issues, rerun until clean or max iterations
- Agent timeout warning handler: inject "5 minutes remaining" messages using `created_at` + `timeout_seconds`

## Testing

- Anthropic-shape agent e2e test. The agent e2e tests (critic/grader/critic_dev) only
  exercise the OpenAI shapes via `props/testing/fake_openai_server.py`; the Anthropic
  `/v1/messages` path is covered only by `props/agents/af/test_client.py` unit/respx tests.
  This gap let two crash bugs ship to live runs (the `store` kwarg the Anthropic SDK rejects,
  and the `/v1/v1/messages` double-path). Add a fake-anthropic-server (analog of
  `fake_openai_server.py`) and an agent e2e that drives `AnthropicClient → props-llm-proxy
/v1/messages → mock backend` so the anthropic path is covered in CI.

## Infrastructure

- Sane story for applying migrations without full `db recreate` (direct `alembic upgrade head`)
- Bulk specimen sync in `props db sync` from Bazel bundle artifacts (currently one-by-one via `sync-specimen`)
- GLM per-model pricing. `cluster/k8s/props/app/config.toml` assigns all 7 z.ai GLM models
  glm-4.6's placeholder rates (input 0.39 / output 1.74 per 1M). Set real-ish per-model z.ai
  rates so budget accounting is accurate. Low urgency: z.ai is currently a flat prepaid
  subscription (quota-based), so per-token cost is largely cosmetic for budgeting.
