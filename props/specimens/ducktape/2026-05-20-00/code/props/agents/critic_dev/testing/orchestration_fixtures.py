"""Shared constants and mocks for multi-model orchestration e2e tests."""

from __future__ import annotations

# Model names for multi-model routing.
# Must exist in synced model_metadata (llm_requests.model has FK to model_metadata.model_id).
# Each must be DISTINCT so multi-model FakeOpenAIServer routes to the right mock.
ORCHESTRATION_OPTIMIZER_MODEL = "gpt-4o"
ORCHESTRATION_CRITIC_MODEL = "gpt-4o-mini"
ORCHESTRATION_GRADER_MODEL = "gpt-4.1-mini"
