"""Test-wide constants.

Must exist in synced model_metadata (llm_requests.model has FK to model_metadata.model_id).
"""

from props.core.models.examples import WholeSnapshotExample

# Cheap model for tests that just need a valid model name.
DEFAULT_TEST_MODEL = "gpt-4o-mini"

# Model with known per-token pricing for budget/cost assertions.
BUDGET_TEST_MODEL = "gpt-5.1"

# Canonical training example for tests.
TRAIN_EXAMPLE = WholeSnapshotExample(snapshot_slug="test-fixtures/train1")
