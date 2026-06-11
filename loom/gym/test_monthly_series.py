from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel

from loom.gym.monthly_series import load_series, validate_known_history


def test_validate_known_history_rejects_bad_data() -> None:
    good = {date(2024, 11, 1): 6032.38, date(2024, 12, 1): 5881.63}
    validate_known_history("sp500", good)
    with pytest.raises(ValueError, match="bad evidence data"):
        validate_known_history("sp500", good | {date(2024, 12, 1): 1234.0})


def test_load_series_requires_evidence_files(tmp_path: Path) -> None:
    # load_series reads the augur-evidence checkout; a missing file must surface,
    # not silently produce an empty series.
    with pytest.raises(RuntimeError, match="evidence not found"):
        load_series(tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
