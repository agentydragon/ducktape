"""Offline path selection through the actual Trinity composition and Python action loop."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import materialize_sampled_exogenous
from finance.augur.study.trinity.replay import (
    BOND_SPEC,
    EQUITY_SPEC,
    HORIZON_MONTHS,
    Situation,
    execute,
    sample_replay,
    situation,
)
from finance.augur.study.trinity.synthetic import synthetic_history


@pytest.fixture
def history() -> MacroHistory:
    return synthetic_history(HORIZON_MONTHS)


def _situation(model: HistoricalWindowsModel, dates: tuple[date, ...]) -> Situation:
    bundle = model.materialize(window_starts=dates, horizon_months=HORIZON_MONTHS)
    return situation(materialize_sampled_exogenous(bundle), rollout_count=len(dates))


def test_trinity_keeps_every_start_and_composes_the_identical_paths(history: MacroHistory, tmp_path: Path) -> None:
    with patch("finance.augur.model.historical_windows.load_macro_history", return_value=history):
        replay = sample_replay(tmp_path)
    assert replay.window_starts == history.months[:4]
    assert replay.window_count == 4
    assert replay.situation == _situation(
        HistoricalWindowsModel(history=history, equity=EQUITY_SPEC, instruments=(BOND_SPEC,)), history.months[:4]
    )


def test_selected_trace_and_outcomes_match_the_same_date_in_a_population(history: MacroHistory) -> None:
    model = HistoricalWindowsModel(history=history, equity=EQUITY_SPEC, instruments=(BOND_SPEC,))
    dates = (history.months[2], history.months[0], history.months[3])
    population = _situation(model, dates)
    selected = _situation(model, (dates[2],))
    summaries = execute(population, equity_share=0.6, withdrawal_rate=0.04, rollout_ids=[0, 1, 2])
    traces = execute(population, equity_share=0.6, withdrawal_rate=0.04, rollout_ids=[2, 0], capture="forensic")
    separate = execute(selected, equity_share=0.6, withdrawal_rate=0.04, rollout_ids=[0], capture="forensic")[0]
    assert [row.rollout_id for row in traces] == [2, 0]
    for trace in traces:
        assert trace.summary == summaries[trace.rollout_id].summary
        assert trace.stop == summaries[trace.rollout_id].stop
    assert separate.summary == traces[0].summary
    assert separate.trace is not None
    original = traces[0].trace
    assert original is not None
    assert separate.trace.receipts == original.receipts
    # Separately materializing one date assigns it local ID 0, not population ID 2.
    assert separate.trace.books == original.books
    assert separate.trace.journal == original.journal
    assert separate.trace.distributions == original.distributions
    assert separate.trace.events.rollout_ids == (0,)
    assert original.events.rollout_ids == (2,)
    for spec in EVENT_FRAME_SPECS:
        assert (
            separate.trace.events.frame(spec).drop("rollout_id").equals(original.events.frame(spec).drop("rollout_id"))
        )
    assert not original.events.obligation_settlements.is_empty()
    assert not original.events.lot_dispositions.is_empty()
    assert len({row.summary.cash[0].values[-1] for row in summaries}) > 1


if __name__ == "__main__":
    pytest_bazel.main()
