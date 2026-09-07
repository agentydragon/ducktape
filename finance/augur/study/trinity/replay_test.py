"""Check augur against the Trinity study's published 30-year table.

The assertions are deliberately not tight. A reproduction that matched to the point would be
suspicious — the equity series, the bond model and the window spacing all differ from the
paper (see `replay.py`) — so what is pinned here is that augur lands in the published
neighbourhood, that it does so for the reason the paper gives (the input returns resemble the
paper's), and that its answer moves the way a withdrawal study's answer must.

Network-dependent and slow: the record is fetched from the public upstreams and every cell is
a 474-rollout simulation. Hence `manual` — see the package README.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence
from finance.augur.study.trinity.replay import (
    EVIDENCE,
    HORIZON_MONTHS,
    PAPER_COMPOUND_RETURN_PERCENT,
    PUBLISHED_RATES,
    SAFEMAX_GRID,
    TABLE_3_SUCCESS_PERCENT,
    Replay,
    sample_replay,
)

# Rates 9% and up are 0% success on both sides for every allocation, so they discriminate
# nothing while costing a simulation each.
CHECKED_RATES = tuple(rate for rate in PUBLISHED_RATES if rate <= 0.08)

# Allocations the "4% rule" is actually about. Trinity's own 4%/30-year success for the
# bond-heavy two is 71% and 20%, so they are outside any band a 4%-rule claim could have.
EQUITY_LED_SHARES = (1.00, 0.75, 0.50)

# Wide, because the bond sleeve is a duration approximation of the paper's corporates and the
# 25/75 row is where that shows. A bound this loose still fails loudly if the reproduction
# stops reproducing; it is not a claim that this is the achievable accuracy.
TABLE_TOLERANCE_POINTS = 20
# Where the model is faithful and the study is about, agreement should be close.
EQUITY_LED_4_PERCENT_TOLERANCE_POINTS = 5


@pytest.fixture(scope="module")
def replay() -> Iterator[Replay]:
    """One sample for the whole module: every test below differs only in the portfolio."""

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(snapshot_evidence(directory, EVIDENCE))
        yield sample_replay(directory)


def test_the_replayed_record_is_the_studys_own_period(replay: Replay) -> None:
    """Everything else here is a claim about 1926-1995. Replaying past 1995 would answer about
    a sample the paper never saw, and the disagreement would be unattributable."""

    assert replay.record_end == date(1995, 12, 1)
    # French's factors start 1926-07, so the record cannot reach the paper's January.
    assert replay.record_start == date(1926, 7, 1)
    # Every window the span supplies, not a thinned subset: a success rate over fewer of them
    # counts a different set of historical starting months than the paper's does.
    record_months = (
        12 * (replay.record_end.year - replay.record_start.year)
        + (replay.record_end.month - replay.record_start.month)
        + 1
    )
    assert replay.window_count == record_months - HORIZON_MONTHS


def test_the_input_returns_resemble_the_papers(replay: Replay) -> None:
    """An anchor on the inputs, not the outputs. A mispriced sleeve can still produce a
    plausible success table, and would then read as a methodology difference forever.

    A sanity bound, not a precision claim: it is here to catch a sleeve earning nothing or
    earning double, which is what a broken price or a dropped coupon looks like. The gaps that
    do exist are 0.3pp on equity and 1.8pp on bonds — see the module docstring, which is where
    the bond one is accounted for.
    """

    for symbol, paper in PAPER_COMPOUND_RETURN_PERCENT.items():
        assert replay.compound_return_percent[symbol] == pytest.approx(paper, abs=3.0), symbol


def test_four_percent_over_thirty_years_fails_less_than_a_tenth_of_the_time(replay: Replay) -> None:
    """The headline claim the 4% rule makes, and the one this reproduction exists to check."""

    for equity_share in EQUITY_LED_SHARES:
        failure = 1.0 - replay.success_rate(equity_share=equity_share, withdrawal_rate=0.04)
        assert 0.0 <= failure <= 0.10, f"{equity_share:.0%} equity failed {failure:.1%} of windows"


def test_the_four_percent_failure_rate_matches_the_published_one(replay: Replay) -> None:
    """Being inside a 0-10% band is weaker than agreeing with Trinity, who put it at 2-5%."""

    published_index = PUBLISHED_RATES.index(0.04)
    for equity_share in EQUITY_LED_SHARES:
        published = TABLE_3_SUCCESS_PERCENT[equity_share][published_index]
        reproduced = 100.0 * replay.success_rate(equity_share=equity_share, withdrawal_rate=0.04)
        assert reproduced == pytest.approx(published, abs=EQUITY_LED_4_PERCENT_TOLERANCE_POINTS), (
            f"{equity_share:.0%} equity: augur {reproduced:.0f}%, Table 3 {published}%"
        )


def test_safemax_lands_where_table_3_puts_it(replay: Replay) -> None:
    """Table 3 resolves SAFEMAX only to "in [3%, 4%)" — 100% success at 3%, less at 4% — for
    every allocation holding equity. A finer grid must still land inside that interval."""

    for equity_share in (*EQUITY_LED_SHARES, 0.25):
        safemax = replay.safemax(equity_share=equity_share, grid=SAFEMAX_GRID)
        assert safemax is not None, f"{equity_share:.0%} equity did not survive the grid's floor"
        assert 0.03 <= safemax < 0.04, f"{equity_share:.0%} equity: SAFEMAX {safemax:.1%}"


def test_the_whole_table_lands_in_the_published_neighbourhood(replay: Replay) -> None:
    """Every allocation that holds equity. The all-bond row sits outside any tolerance this
    could set and gets its own directional test below — see the module docstring."""

    for equity_share, published in TABLE_3_SUCCESS_PERCENT.items():
        if equity_share == 0.0:
            continue
        for rate, paper in zip(PUBLISHED_RATES, published, strict=True):
            if rate not in CHECKED_RATES:
                continue
            reproduced = 100.0 * replay.success_rate(equity_share=equity_share, withdrawal_rate=rate)
            assert reproduced == pytest.approx(paper, abs=TABLE_TOLERANCE_POINTS), (
                f"{equity_share:.0%} equity at {rate:.0%}: augur {reproduced:.0f}%, Table 3 {paper}%"
            )


def test_the_all_bond_row_stays_under_the_published_one(replay: Replay) -> None:
    """Pins the direction and the ceiling of an OPEN discrepancy — see the module docstring.

    This row sits far below Table 3 (43% against 80% at a 3% withdrawal) for reasons not yet
    accounted for. What is known is the sign: every difference measured so far — the coupon
    parked in cash rather than reinvested, the start-of-year withdrawal — is a drag, so this
    row can only come in under the paper's, never over.

    An inequality and not a tolerance, because the ceiling is the falsifiable half: the
    previous instrument model over-distributed and put this row at 84% against the paper's 80%,
    which this catches and a symmetric band would not. A floor would only pin the discrepancy
    in place, and it is meant to close.
    """

    published = TABLE_3_SUCCESS_PERCENT[0.0]
    for rate, paper in zip(PUBLISHED_RATES, published, strict=True):
        if rate not in CHECKED_RATES:
            continue
        reproduced = 100.0 * replay.success_rate(equity_share=0.0, withdrawal_rate=rate)
        assert reproduced <= paper, f"all-bond at {rate:.0%}: augur {reproduced:.0f}%, Table 3 {paper}%"


def test_raising_the_withdrawal_never_raises_the_success_rate(replay: Replay) -> None:
    """A structural invariant rather than a fact about history: a larger withdrawal takes
    strictly more out of the portfolio in every month of every window, so it cannot rescue a
    window that a smaller one exhausted. It is also what makes `safemax`'s bisection valid."""

    for equity_share in (1.00, 0.50, 0.00):
        rates = [replay.success_rate(equity_share=equity_share, withdrawal_rate=rate) for rate in CHECKED_RATES]
        assert rates == sorted(rates, reverse=True), f"{equity_share:.0%} equity: {rates}"


if __name__ == "__main__":
    pytest_bazel.main()
