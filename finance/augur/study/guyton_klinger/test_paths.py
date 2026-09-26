"""Annual embedding and window identity, read back from the compiled market paths."""

from collections.abc import Sequence

import pytest
import pytest_bazel

from finance.augur.sim.market_path import MarketPath
from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve
from finance.augur.study.guyton_klinger.paths import annual_windows


@pytest.fixture
def panel() -> AnnualPanel:
    return AnnualPanel(
        first_year=1990,
        returns={Sleeve.CASH: (0.0, 0.0, 0.0), Sleeve.BONDS: (0.0, 0.0, 0.0), Sleeve.EQUITY: (0.1, -0.2, 0.5)},
        inflation=(0.03, 0.02, 0.01),
    )


def test_levels_hold_within_each_year_and_rollout_ids_follow_requested_start_order(panel: AnnualPanel) -> None:
    windows = annual_windows(panel, start_years=[1991, 1990], years=2)
    assert windows.start_years == (1991, 1990)
    later, earlier = (MarketPath(windows.series, id_, rollout_count=2) for id_ in (0, 1))
    # Micro-dollar prices from $1 and parts-per-billion CPI; the terminal mark carries the last year.
    assert later.path("security:equity") == [1_000_000] * 12 + [800_000] * 12 + [1_200_000]
    assert later.path("inflation") == [10**9] * 12 + [1_020_000_000] * 12 + [1_030_200_000]
    assert earlier.path("security:equity") == [1_000_000] * 12 + [1_100_000] * 12 + [880_000]
    assert earlier.path("security:cash") == [1_000_000] * 25


@pytest.mark.parametrize(
    ("start_years", "years", "match"),
    [
        pytest.param([1992], 2, "complete years", id="past-end"),
        pytest.param([1989], 1, "complete years", id="before-start"),
        pytest.param([1990, 1990], 1, "distinct", id="duplicate"),
        pytest.param([], 1, "nonempty", id="none"),
        pytest.param([1990], 0, "positive", id="empty-window"),
    ],
)
def test_rejects_windows_the_panel_does_not_complete(
    panel: AnnualPanel, start_years: Sequence[int], years: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        annual_windows(panel, start_years=start_years, years=years)


if __name__ == "__main__":
    pytest_bazel.main()
