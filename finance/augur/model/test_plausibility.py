"""Controls for the plausibility gate.

The band file is a FIXTURE (`testdata/plausibility_bands_fixture.yaml`), not sourced evidence.
The replay control runs `HistoricalWindowsModel` over the test-only placeholder record in
`synthetic_history.py`, because the real record needs a network fetch: it shows the gate lets
sane replayed paths through, not that the real past satisfies sourced bands.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.historical_windows import HistoricalWindowsModel
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.plausibility import (
    Band,
    BandFile,
    BreachDirection,
    Breached,
    GateResult,
    Limit,
    Measure,
    OverriddenRefusal,
    Override,
    Passed,
    RefusalError,
    Severity,
    Unmodeled,
    Verdict,
    evaluate,
    require_plausible,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.structural_macro import EquityProcess, MacroVarSpec, StructuralMacroProviderConfig
from finance.augur.model.synthetic_history import synthetic_history
from util.bazel.runfiles import get_required_path

HORIZON_MONTHS = 24
ROLLOUTS = 1000
EQUITY_MEASURES = {Measure.NOMINAL_EQUITY_WEALTH_FACTOR, Measure.REAL_EQUITY_WEALTH_FACTOR}

# Short rate, term spread and inflation start at their long-run means (3%, 1%, 2.5%) with small
# shocks, so the rate and inflation bands pass and any refusal comes from the equity process.
BENIGN_MACRO = MacroVarSpec(
    initial_state=(0.03, 0.01, 0.025),
    intercept=(0.003, 0.001, 0.0025),
    transition=((0.9, 0.0, 0.0), (0.0, 0.9, 0.0), (0.0, 0.0, 0.9)),
    shock_cholesky=((0.002, 0.0, 0.0), (0.0, 0.001, 0.0), (0.0, 0.0, 0.002)),
)
# A 5%/month drift: about 1% of paths multiply real equity wealth tenfold within two years.
EXPLOSIVE_EQUITY = EquityProcess(
    instrument=EquitySpec(symbol=SecuritySymbol("test_equity"), initial_price_usd=100.0),
    monthly_log_return_mu=0.05,
    monthly_log_return_sigma=0.10,
)


def _structural_market(equity: EquityProcess | None) -> MarketPaths:
    model = StructuralMacroProviderConfig(macro_state=BENIGN_MACRO, equity=equity).realize_model()
    return model.sample_market(
        ExogenousSamplingRequest(horizon_months=HORIZON_MONTHS, rollout_seeds=tuple(range(ROLLOUTS)))
    )


def _twelve_month_paths(*, cpi_growth: float, equity_growth: float | None = None) -> MarketPaths:
    """Three identical rollouts compounding to the given factors at month 12; rates flat at 3% + 1%."""

    ramp = np.arange(13) / 12.0
    return MarketPaths(
        short_rate=np.full((3, 13), 0.03),
        term_spread=np.full((3, 13), 0.01),
        cpi_level=np.tile(100.0 * cpi_growth**ramp, (3, 1)),
        equity_total_return_index=None if equity_growth is None else np.tile(equity_growth**ramp, (3, 1)),
        corporate_yields={},
        model_id="test_model",
        provenance={},
    )


def _band(
    measure: Measure = Measure.INFLATION_FACTOR,
    *,
    percentile: float,
    lower: Limit | None = None,
    upper: Limit | None = None,
    severity: Severity = Severity.FLAG,
) -> Band:
    return Band(
        measure=measure,
        horizon_months=12,
        percentile=percentile,
        lower=lower,
        upper=upper,
        severity=severity,
        source="test: hand-set band",
    )


@pytest.fixture
def bands() -> BandFile:
    return BandFile.from_yaml(get_required_path("_main/finance/augur/model/testdata/plausibility_bands_fixture.yaml"))


@pytest.fixture
def unmodeled_and_breached() -> GateResult:
    """Paths with no equity and 50% inflation against two REFUSE bands, blocking in that order."""

    return evaluate(
        _twelve_month_paths(cpi_growth=1.5),
        BandFile(
            bands=(
                _band(
                    Measure.REAL_EQUITY_WEALTH_FACTOR,
                    percentile=50,
                    upper=Limit(value=2.0, breach=BreachDirection.TOO_OPTIMISTIC),
                    severity=Severity.REFUSE,
                ),
                _band(
                    percentile=99,
                    upper=Limit(value=1.3, breach=BreachDirection.TOO_PESSIMISTIC),
                    severity=Severity.REFUSE,
                ),
            )
        ),
    )


def test_an_explosive_equity_drift_is_refused(bands: BandFile) -> None:
    market = _structural_market(EXPLOSIVE_EQUITY)
    assert market.equity_total_return_index is not None
    real = market.equity_total_return_index / (market.cpi_level / market.cpi_level[:, :1])
    # The absurdity the gate exists for, far above the fixture's 1-in-10,000.
    assert np.mean(real[:, HORIZON_MONTHS] >= 10.0) > 0.001

    with pytest.raises(RefusalError) as refused:
        require_plausible(evaluate(market, bands))

    assert refused.value.refusals
    for outcome in refused.value.refusals:
        assert isinstance(outcome, Breached)
        assert outcome.verdict is Verdict.REFUSE
        assert outcome.band.measure is Measure.REAL_EQUITY_WEALTH_FACTOR
        assert outcome.limit.breach is BreachDirection.TOO_OPTIMISTIC


def test_historical_replay_passes_every_band(bands: BandFile) -> None:
    replay = HistoricalWindowsModel(history=synthetic_history(months=600))
    market = replay.market_paths(window_starts=replay.window_starts(HORIZON_MONTHS), horizon_months=HORIZON_MONTHS)
    result = evaluate(market, bands)

    assert all(isinstance(outcome, Passed) for outcome in result.outcomes), str(result)
    require_plausible(result)


def test_equity_bands_on_a_model_without_equity_are_unmodeled_not_passed(bands: BandFile) -> None:
    result = evaluate(_structural_market(equity=None), bands)

    for outcome in result.outcomes:
        assert isinstance(outcome, Unmodeled) == (outcome.band.measure in EQUITY_MEASURES), str(outcome)
    refusing = {band for band in bands.bands if band.measure in EQUITY_MEASURES and band.severity is Severity.REFUSE}
    assert refusing  # without a refusing equity band the check below proves nothing

    # An unmodeled REFUSE band blocks; an unmodeled FLAG band is only reported.
    with pytest.raises(RefusalError) as refused:
        require_plausible(result)
    assert {outcome.band for outcome in refused.value.refusals} == refusing


def test_an_override_accepts_only_the_bands_it_names(unmodeled_and_breached: GateResult) -> None:
    unmodeled, breached = unmodeled_and_breached.blocking

    with pytest.raises(RefusalError) as refused:
        require_plausible(
            unmodeled_and_breached, override=Override(bands=frozenset({unmodeled.band}), reason="test: partial")
        )

    assert refused.value.refusals == (breached,)


def test_accepted_overrides_come_back_as_records_and_are_logged(
    unmodeled_and_breached: GateResult, caplog: pytest.LogCaptureFixture
) -> None:
    unmodeled, breached = unmodeled_and_breached.blocking
    assert isinstance(breached, Breached)  # so a record carries a checked value and its limit
    everything = Override(bands=frozenset({unmodeled.band, breached.band}), reason="test: all")

    with caplog.at_level(logging.WARNING, logger="finance.augur.model.plausibility"):
        records = require_plausible(unmodeled_and_breached, override=everything)

    assert records == (
        OverriddenRefusal(outcome=unmodeled, reason="test: all"),
        OverriddenRefusal(outcome=breached, reason="test: all"),
    )
    for log, record in zip(caplog.records, records, strict=True):
        assert log.levelno == logging.WARNING
        assert str(record) in log.getMessage()


def test_each_measure_reads_its_definition_off_the_paths() -> None:
    wide = Limit(value=10.0, breach=BreachDirection.TOO_WIDE)
    result = evaluate(
        _twelve_month_paths(cpi_growth=1.2, equity_growth=1.5),
        BandFile(bands=tuple(_band(measure, percentile=50, upper=wide) for measure in Measure)),
    )

    values = {outcome.band.measure: outcome.value for outcome in result.outcomes if isinstance(outcome, Passed)}
    assert values == pytest.approx(
        {
            Measure.NOMINAL_EQUITY_WEALTH_FACTOR: 1.5,
            Measure.REAL_EQUITY_WEALTH_FACTOR: 1.5 / 1.2,
            Measure.INFLATION_FACTOR: 1.2,
            Measure.SHORT_RATE: 0.03,
            Measure.TEN_YEAR_YIELD: 0.04,
        }
    )


def test_a_flag_breach_reports_the_crossed_limit_without_blocking() -> None:
    too_little = _band(percentile=40, lower=Limit(value=1.2, breach=BreachDirection.TOO_OPTIMISTIC))
    too_much = _band(percentile=60, upper=Limit(value=1.05, breach=BreachDirection.TOO_PESSIMISTIC))
    result = evaluate(_twelve_month_paths(cpi_growth=1.1), BandFile(bands=(too_little, too_much)))

    assert [outcome.limit for outcome in result.outcomes if isinstance(outcome, Breached)] == [
        too_little.lower,
        too_much.upper,
    ]
    assert [outcome.verdict for outcome in result.outcomes] == [Verdict.FLAG, Verdict.FLAG]
    require_plausible(result)


def test_paths_shorter_than_the_bands_are_rejected(bands: BandFile) -> None:
    with pytest.raises(ValueError, match="bands reach month 24 but test_model paths end at month 12"):
        evaluate(_twelve_month_paths(cpi_growth=1.02), bands)


@pytest.mark.parametrize(
    ("lower", "upper", "message"),
    [
        (None, None, "has no limit"),
        (
            Limit(value=1.2, breach=BreachDirection.TOO_OPTIMISTIC),
            Limit(value=1.1, breach=BreachDirection.TOO_PESSIMISTIC),
            "is above upper",
        ),
    ],
)
def test_a_band_needs_an_ordered_limit(lower: Limit | None, upper: Limit | None, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _band(percentile=50, lower=lower, upper=upper)


def test_a_band_file_rejects_two_bands_on_one_statistic() -> None:
    band = _band(percentile=50, upper=Limit(value=1.1, breach=BreachDirection.TOO_PESSIMISTIC))
    with pytest.raises(ValidationError, match="repeat"):
        BandFile(bands=(band, band.model_copy(update={"source": "test: a second opinion"})))


if __name__ == "__main__":
    pytest_bazel.main()
