"""Check augur's bond fund against Ibbotson's published long-term corporate returns.

Two things are pinned. That the construction reproduces a real century of a published series
at all — which is what licenses using it for anything, `study/trinity` included. And that
maturity 20 is what the data says, because the cheapest way to make Trinity's all-bond row
agree is to shorten the maturity, and that is fitting the answer rather than the instrument
(see `finance/augur/debug/trinity_all_bond_row.md`).

Network-dependent: the Aaa yields are fetched from FRED. Hence `manual` — see the package
README.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.study.sbbi.long_term_corporate import (
    EVIDENCE,
    PUBLISHED_ANNUAL_RETURN,
    PUBLISHED_SUMMARY_PERCENT,
    compared_years,
    compound_percent,
    reproduce,
    tracking_error,
)
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence

# The published series is stated to a tenth of a point and prices a different yield universe
# (see the module docstring), so a tight bound would be measuring the yield-source difference
# rather than the arithmetic. This is a sanity ceiling, not a precision claim.
TRACKING_ERROR_CEILING_POINTS = 4.0

# The known defect: Moody's Aaa is higher-grade than the Aaa-and-Aa composite Ibbotson priced,
# so the sleeve's coupon runs light. Bounded rather than asserted equal, and bounded on BOTH
# sides — a reproduction that started running OVER the published series would mean something
# else broke, and drifting further under is the regression this exists to catch.
KNOWN_SHORTFALL_POINTS = (0.0, 0.5)


@pytest.fixture(scope="module")
def reproduced() -> Iterator[dict[int, float]]:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(snapshot_evidence(directory, EVIDENCE))
        yield reproduce(directory)


def test_the_published_series_was_read_correctly(reproduced: dict[int, float]) -> None:
    """The vendored annual returns are the diagonal of a holding-period matrix, so a misread row
    is the live risk. The monograph's own summary statistics are computed from all 62 years and
    published separately, so agreeing with them is an independent check on the transcription —
    and it is a fact about the literals, which is why it does not touch `reproduced`."""

    years = sorted(PUBLISHED_ANNUAL_RETURN)
    published = np.array([PUBLISHED_ANNUAL_RETURN[y] for y in years])
    assert compound_percent(PUBLISHED_ANNUAL_RETURN, years) == pytest.approx(
        PUBLISHED_SUMMARY_PERCENT["geometric"], abs=0.1
    )
    assert 100.0 * float(published.mean()) == pytest.approx(PUBLISHED_SUMMARY_PERCENT["arithmetic"], abs=0.1)
    assert 100.0 * float(published.std(ddof=1)) == pytest.approx(
        PUBLISHED_SUMMARY_PERCENT["standard_deviation"], abs=0.1
    )


def test_the_fund_reproduces_a_published_century_of_corporate_bond_returns(reproduced: dict[int, float]) -> None:
    """The external check. Every other test of `bond_fund` is either a one-period identity or an
    internal invariant; this is the only one that runs the construction over a real century and
    compares it to what somebody else published from the same history."""

    assert len(compared_years(reproduced)) >= 60
    assert tracking_error(reproduced) < TRACKING_ERROR_CEILING_POINTS


def test_the_sleeve_runs_slightly_under_the_published_series(reproduced: dict[int, float]) -> None:
    """Bounds a named defect so it cannot quietly widen. Closing it needs a yield series between
    Moody's Aaa and Baa spanning 1926-1987, which is not public."""

    years = compared_years(reproduced)
    shortfall = compound_percent(PUBLISHED_ANNUAL_RETURN, years) - compound_percent(reproduced, years)
    low, high = KNOWN_SHORTFALL_POINTS
    assert low <= shortfall <= high, f"sleeve runs {shortfall:+.2f} points a year against the published series"


def test_the_data_prefers_the_maturity_the_monograph_documents(reproduced: dict[int, float]) -> None:
    """The anti-fitting guard. Maturity 10 lands Trinity's all-bond row on its published value
    almost exactly and is wrong; the actual return series rejects it. Asserted as an ordering
    rather than an optimum, because the fit is flat between 20 and 25 and pinning an argmin
    there would be pinning noise."""

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(snapshot_evidence(directory, EVIDENCE))
        errors = {m: tracking_error(reproduce(directory, maturity_years=m)) for m in (5.0, 10.0, 15.0)}

    documented = tracking_error(reproduced)
    for maturity, error in errors.items():
        assert documented < error, f"maturity {maturity:.0f} tracks better ({error:.2f}) than 20 ({documented:.2f})"


if __name__ == "__main__":
    pytest_bazel.main()
