"""Fixtures the suites share.

Only the ones that would otherwise make every test take `tmp_path` and hand it on live
here. A fixture the caller varies — a rollout count, a sale on or off — stays a plain
function in `fixtures.py`, because parameterizing a pytest fixture costs more indirection
than the call site it saves.
"""

from pathlib import Path
from typing import Any

import pytest

from finance.augur.rust.differential.fixtures import feature_rich_fixture


@pytest.fixture
def feature_rich(tmp_path: Path) -> dict[str, Any]:
    """The generated feature-rich scenario at its default rollout count."""

    return feature_rich_fixture(tmp_path)
