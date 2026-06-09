from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel

from loom.gym.model_cutoffs import assert_admissible


def test_unknown_model_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        assert_admissible(model_id="gpt-best", as_of=date(2024, 7, 1))


def test_model_with_cutoff_after_as_of_rejected() -> None:
    # glm-4.5's weights may contain early-2024 outcomes, so it may not forecast as-of January 2024.
    with pytest.raises(ValueError, match="inadmissible"):
        assert_admissible(model_id="glm-4.5", as_of=date(2024, 1, 1))


def test_admissible_model_returns_cutoff_entry() -> None:
    entry = assert_admissible(model_id="glm-4.5", as_of=date(2024, 7, 1))
    assert entry.cutoff <= date(2024, 7, 1)
    assert "pm_reifier" in entry.provenance


if __name__ == "__main__":
    pytest_bazel.main()
