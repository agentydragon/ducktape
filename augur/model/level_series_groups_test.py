from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.model.level_series_groups import LevelSeriesGroups
from augur.model.series import CryptoKey, HomeValueKey, InflationKey, RentKey, SP500Key


def _groups(**fields: object) -> LevelSeriesGroups[int]:
    return LevelSeriesGroups[int].model_validate(fields)


def test_by_level_key_projects_each_kind_to_its_typed_key() -> None:
    groups = _groups(
        inflation=1, sp500=2, crypto={"btc": 3, "eth": 4}, home_value={"san_francisco_ca": 5}, rent={"vallejo_ca": 6}
    )
    assert groups.by_level_key() == {
        InflationKey(): 1,
        SP500Key(): 2,
        CryptoKey(symbol="btc"): 3,
        CryptoKey(symbol="eth"): 4,
        HomeValueKey(location_id="san_francisco_ca"): 5,
        RentKey(location_id="vallejo_ca"): 6,
    }


def test_by_level_key_omits_absent_singletons() -> None:
    # Absent singleton ⇒ no key at all (the series is unmodeled), not a key with
    # a None/zero value — this distinction is why singletons are `ValueT | None`.
    assert _groups(sp500=2).by_level_key() == {SP500Key(): 2}


def test_extra_forbid_rejects_legacy_prefix_keys() -> None:
    # The point of the typed shape: an old-style wire-id key must fail loudly at
    # load, not be silently accepted or dropped.
    with pytest.raises(ValidationError):
        LevelSeriesGroups[int].model_validate({"crypto:btc": 1})


if __name__ == "__main__":
    pytest_bazel.main()
