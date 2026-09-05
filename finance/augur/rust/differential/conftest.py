"""Fixtures the suites share.

Only the ones a test would otherwise restate live here. A case the caller varies — a
rollout count, a sale on or off — stays a plain function in `fixtures.py`, because
parameterizing a pytest fixture costs more indirection than the call site it saves.
"""

import pytest

from finance.augur.benchmark.scenario import MIN_FEATURE_HORIZON_MONTHS, feature_rich_case
from finance.augur.sim.testing.case import Case


@pytest.fixture
def feature_rich() -> Case:
    """The generated feature-rich scenario at its default rollout count."""

    return feature_rich_case(rollout_count=4, horizon_months=MIN_FEATURE_HORIZON_MONTHS)
