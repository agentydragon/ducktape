from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel

from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS, assert_admissible


def test_unknown_model_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        assert_admissible(model_id="gpt-best", as_of=date(2024, 7, 1))


def test_model_with_cutoff_after_as_of_rejected() -> None:
    # glm-4.5's weights may contain early-2024 outcomes, so it may not forecast as-of January 2024.
    with pytest.raises(ValueError, match="inadmissible"):
        assert_admissible(model_id="glm-4.5", as_of=date(2024, 1, 1))


def test_admissible_model_returns_cutoff_entry() -> None:
    entry = assert_admissible(model_id="glm-4.5", as_of=date(2024, 7, 1))
    assert entry.knowledge_cutoff <= date(2024, 7, 1)
    assert "pm_reifier" in entry.provenance


def test_strict_mode_bounds_by_weights_release() -> None:
    # glm-4.5 passes the probed-cutoff bound for mid-2024 tasks, but its weights
    # shipped in 2025 — strict mode rejects anything earlier than that.
    assert_admissible(model_id="glm-4.5", as_of=date(2024, 7, 1))
    with pytest.raises(ValueError, match="weights-release"):
        assert_admissible(model_id="glm-4.5", as_of=date(2024, 7, 1), strict=True)
    assert_admissible(model_id="glm-4.5", as_of=date(2025, 8, 1), strict=True)


def test_weights_release_never_precedes_knowledge_cutoff() -> None:
    # A model cannot know more than its frozen weights contain; an entry
    # violating this ordering is a registry typo.
    for entry in KNOWN_MODEL_CUTOFFS.values():
        assert entry.knowledge_cutoff <= entry.weights_released


if __name__ == "__main__":
    pytest_bazel.main()
