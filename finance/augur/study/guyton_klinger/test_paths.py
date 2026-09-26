"""Annual embedding and window identity, read back from the compiled market paths."""

from collections.abc import Sequence

import pytest
import pytest_bazel

from finance.augur.sim.market_path import MarketPath
from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve
from finance.augur.study.guyton_klinger.paths import Taxes, annual_windows

# Micro-dollar prices from $1, parts-per-billion CPI, and per-unit payouts in nano-quanta.
DOLLAR = 1_000_000


@pytest.fixture
def panel() -> AnnualPanel:
    # Equity total returns 10%, -20%, 50%.
    return AnnualPanel(
        first_year=1990,
        income={Sleeve.CASH: (0.05, 0.04, 0.03), Sleeve.BONDS: (0.0, 0.0, 0.0), Sleeve.EQUITY: (0.02, 0.03, 0.0)},
        price={Sleeve.BONDS: (0.0, 0.0, 0.0), Sleeve.EQUITY: (0.08, -0.23, 0.5)},
        inflation=(0.03, 0.02, 0.01),
    )


def test_untaxed_levels_carry_total_returns_held_within_each_year_in_requested_start_order(panel: AnnualPanel) -> None:
    windows = annual_windows(panel, start_years=[1991, 1990], years=2, taxes=Taxes.NONE)
    assert windows.start_years == (1991, 1990)
    later, earlier = (MarketPath(windows.series, id_, rollout_count=2) for id_ in (0, 1))
    # The terminal mark carries the last year.
    assert later.path("security:equity") == [DOLLAR] * 12 + [800_000] * 12 + [1_200_000]
    assert later.path("inflation") == [10**9] * 12 + [1_020_000_000] * 12 + [1_030_200_000]
    assert earlier.path("security:equity") == [DOLLAR] * 12 + [1_100_000] * 12 + [880_000]
    assert earlier.path("security:cash") == [DOLLAR] * 12 + [1_050_000] * 12 + [1_092_000]
    assert "security_distribution:equity" not in earlier.series


def test_taxed_levels_carry_price_returns_and_each_year_pays_its_income_in_december(panel: AnnualPanel) -> None:
    windows = annual_windows(panel, start_years=[1991], years=2, taxes=Taxes.FEDERAL_CA)
    window = MarketPath(windows.series, 0, rollout_count=1)
    assert window.path("security:equity") == [DOLLAR] * 12 + [770_000] * 12 + [1_155_000]
    assert window.path("security:cash") == [DOLLAR] * 25
    # 3% of the $1 held through 1991; 1992's equity pays nothing. Bills pay 4% then 3% of their flat $1.
    assert window.path("security_distribution:equity") == [0] * 11 + [30_000 * 10**9] + [0] * 13
    assert window.path("security_distribution:cash") == [0] * 11 + [40_000 * 10**9] + [0] * 11 + [30_000 * 10**9, 0]


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
        annual_windows(panel, start_years=start_years, years=years, taxes=Taxes.NONE)


if __name__ == "__main__":
    pytest_bazel.main()
