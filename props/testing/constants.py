"""Test-wide model constants.

Must exist in synced model_metadata (llm_requests.model has FK to model_metadata.model_id).
"""

# Cheap model for tests that just need a valid model name.
DEFAULT_TEST_MODEL = "gpt-4o-mini"

# Model with known per-token pricing for budget/cost assertions.
BUDGET_TEST_MODEL = "gpt-5.1"
