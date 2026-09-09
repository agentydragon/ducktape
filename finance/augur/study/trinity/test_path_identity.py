"""Offline path-selection checks through the actual Trinity compiler and Rust engine."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest
import pytest_bazel
from polars.testing import assert_frame_equal

from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory
from finance.augur.rust.backend import RustEngine
from finance.augur.sim.backend import CompiledRun, compile_run
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import materialize_sampled_exogenous
from finance.augur.study.trinity.replay import (
    BOND_SPEC,
    EQUITY_SPEC,
    HORIZON_MONTHS,
    RETIREE,
    build_scenario,
    sample_replay,
)


@pytest.fixture
def history() -> MacroHistory:
    index = np.arange(HORIZON_MONTHS + 4)
    return MacroHistory(
        months=tuple(date(1930 + int(i) // 12, int(i) % 12 + 1, 1) for i in index),
        short_rate=0.01 + index * 0.0001,
        term_spread=np.full(len(index), 0.02),
        corporate_aaa_yield=0.04 + index * 0.0001,
        corporate_baa_yield=0.05 + index * 0.0001,
        equity_level=100.0 * np.exp(np.cumsum(0.001 + index * 0.00001)),
        cpi_level=100.0 * np.exp(np.cumsum(0.001 + index * 0.000003)),
    )


def _compile(model: HistoricalWindowsModel, dates: tuple[date, ...]) -> CompiledRun:
    bundle = model.materialize(window_starts=dates, horizon_months=HORIZON_MONTHS)
    return compile_run(
        build_scenario(equity_share=0.6, withdrawal_rate=0.04),
        rollout_count=len(dates),
        external_series=materialize_sampled_exogenous(bundle),
        jurisdictions={},
        locations={},
    )


def test_trinity_keeps_every_start_and_compiles_the_identical_paths(history: MacroHistory, tmp_path: Path) -> None:
    with patch("finance.augur.model.historical_windows.load_macro_history", return_value=history):
        replay = sample_replay(tmp_path)
    assert replay.window_starts == history.months[:4]
    assert replay.window_count == 4
    expected = _compile(
        HistoricalWindowsModel(history=history, equity=EQUITY_SPEC, instruments=(BOND_SPEC,)), history.months[:4]
    )
    actual = compile_run(
        build_scenario(equity_share=0.6, withdrawal_rate=0.04),
        rollout_count=replay.window_count,
        external_series=replay.external_series,
        jurisdictions={},
        locations={},
    )
    assert actual.execution_input == expected.execution_input


def test_selected_trace_and_metrics_match_the_same_date_in_a_population(history: MacroHistory) -> None:
    model = HistoricalWindowsModel(history=history, equity=EQUITY_SPEC, instruments=(BOND_SPEC,))
    dates = (history.months[2], history.months[0], history.months[3])
    population = _compile(model, dates)
    selected = _compile(model, (dates[2],))
    engine = RustEngine()
    all_metrics = engine.product_metrics(population, primary_agent_id=RETIREE)
    one_metrics = engine.product_metrics(selected, primary_agent_id=RETIREE)
    assert len(set(all_metrics.metric_arrays()["net_worth_quanta"][-1])) == len(dates)
    np.testing.assert_array_equal(one_metrics.failed_month, all_metrics.failed_month[2:3])
    for all_values, one_values in zip(all_metrics.base_series, one_metrics.base_series, strict=True):
        np.testing.assert_array_equal(one_values, all_values[:, 2:3])
    all_events = engine.events(population)
    one_events = engine.events(selected)
    assert not one_events.obligation_settlements.is_empty()
    assert not one_events.lot_dispositions.is_empty()
    for spec in EVENT_FRAME_SPECS:
        assert_frame_equal(
            one_events.frame(spec).drop("rollout_index"),
            all_events.frame(spec).filter(pl.col("rollout_index") == 2).drop("rollout_index"),
        )


if __name__ == "__main__":
    pytest_bazel.main()
