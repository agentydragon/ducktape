"""What every simulation engine must do, asserted once and run against each of them.

The suite is the contract in `sim/backend.py` made executable. A new engine gets a module
that names it and inherits from here; it should not need tests of its own, because what it
has to satisfy is not particular to it.

That is a different claim from the differential harness's. Comparing two engines finds where
they disagree and is blind to a rule both implement the same way and both get wrong — which
has happened here. These assertions state what the answer must be, so an engine can fail them
alone or the engines can fail them together, and either is informative.

Assert the contract, not an engine's arithmetic: shapes, schemas, and consequences the
scenario forces regardless of who computes them. A number that only one engine's rounding
produces belongs in that engine's own test.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.metric_composition import METRIC_NAMES
from finance.augur.sim.backend import CompiledRun, Engine
from finance.augur.sim.compiler.plan import compile_simulation
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    ScheduledAssetSale,
    TaxProfile,
)

AGENT = "alice"
HORIZON_MONTHS = 30
SALE_MONTH = 14
UNITS = 2.0
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)
VTI = SecurityKey(symbol=SecuritySymbol("vti"))


def sale_and_tax_year() -> CompiledRun:
    """One long-term lot sold mid-horizon, and the tax year that closes after it.

    Small on purpose. The contract below is about shapes, schemas and consequences the
    scenario forces, and a case whose rows a reader can count says more about a violation
    than a feature-rich one.
    """

    scenario = Scenario(
        agents=[Agent(agent_id=AGENT), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
            for agent_id in (AGENT, "irs")
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice-vti",
                agent_id=AGENT,
                account_id="checking",
                asset=VTI,
                purchase_month_index=-24,  # comfortably long-term
                quantity=UNITS,
                cost_basis_per_unit=LOT_BASIS,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=SALE_MONTH,
                cause_id="sell-vti",
                agent_id=AGENT,
                source_account_id="checking",
                asset=VTI,
                quantity=UNITS,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[TaxProfile(agent_id=AGENT, jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        horizon_months=HORIZON_MONTHS,
    )
    external_series = ExternalSeriesContext.from_level_blocks(
        [(VTI, np.full((1, HORIZON_MONTHS + 1), float(SALE_PRICE)))], rollout_count=1, horizon_months=HORIZON_MONTHS
    )
    jurisdictions = load_jurisdictions_for(scenario)
    return CompiledRun(
        scenario=scenario,
        plan=compile_simulation(
            scenario, rollout_count=1, external_series=external_series, jurisdictions=jurisdictions, locations={}
        ),
        external_series=external_series,
        jurisdictions=jurisdictions,
        locations={},
    )


class EngineAcceptance:
    """Inherit and supply `engine`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def engine(self) -> Engine:
        raise NotImplementedError("an acceptance module names the engine it runs")

    @pytest.fixture(scope="class")
    def run(self) -> CompiledRun:
        return sale_and_tax_year()

    def test_it_says_which_engine_it_is(self, engine: Engine) -> None:
        assert engine.name, "an engine identifies itself in responses and test failures"

    def test_events_carry_every_canonical_frame(self, engine: Engine, run: CompiledRun) -> None:
        """A frame an engine omits reads downstream as "nothing happened", not as a gap."""

        events = engine.events(run)
        for spec in EVENT_FRAME_SPECS:
            frame = getattr(events, spec.name)
            assert isinstance(frame, pl.DataFrame), f"{spec.name} is not a frame"
            assert frame.schema == spec.schema, f"{spec.name} does not match its declared schema"

    def test_the_scheduled_sale_is_reported_as_a_disposition(self, engine: Engine, run: CompiledRun) -> None:
        """Proceeds and basis follow from the scenario, so every engine owes the same ones."""

        rows = engine.events(run).lot_dispositions.filter(pl.col("month_index") == SALE_MONTH).to_dicts()
        assert len(rows) == 1, f"one lot sold once, got {len(rows)} rows"
        sold = rows[0]
        assert sold["agent_id"] == AGENT
        assert sold["units_sold"] == UNITS
        assert sold["proceeds_quanta"] == int(SALE_PRICE * 100) * UNITS
        assert sold["cost_basis_consumed_quanta"] == int(LOT_BASIS * 100) * UNITS

    def test_the_gain_is_assessed_at_the_tax_year_that_closes_after_it(self, engine: Engine, run: CompiledRun) -> None:
        """A realized gain reaches an accrual. Which figure is the statute suites' business."""

        accruals = engine.events(run).tax_accruals.filter(pl.col("agent_id") == AGENT)
        assert accruals.height, "a long-term gain went unassessed"
        assert accruals.filter(pl.col("month_index") > SALE_MONTH).height, "no accrual after the sale"

    def test_product_metrics_cover_every_metric_the_product_renders(self, engine: Engine, run: CompiledRun) -> None:
        """An engine supplies the seven base series; all ten come back.

        The derived three are composed by shared Python, so this also says the composition
        runs over whatever engine produced the base — which is the property that makes one
        engine's fan equal another's.
        """

        metrics = engine.product_metrics(run, primary_agent_id=AGENT)
        arrays = metrics.metric_arrays()
        assert set(arrays) == {"month_index", *METRIC_NAMES}
        assert len(arrays["month_index"]) == HORIZON_MONTHS + 1
        for name in METRIC_NAMES:
            assert arrays[name].shape == (HORIZON_MONTHS + 1, 1), f"{name} is not snapshots by rollouts"
        assert metrics.failed_month.shape == (1,)
        assert metrics.currency_code == run.scenario.currency.code

    def test_a_funded_rollout_does_not_report_a_failure(self, engine: Engine, run: CompiledRun) -> None:
        """Anti-vacuity for the assertions above: they describe a rollout that ran to the end."""

        assert int(engine.product_metrics(run, primary_agent_id=AGENT).failed_month[0]) < 0
        assert engine.events(run).rollout_failures.height == 0

    def test_the_fan_is_ordered_and_agrees_with_the_terminal_samples(self, engine: Engine, run: CompiledRun) -> None:
        """The two reductions are of one population, so the fan must sit inside its range.

        An engine that reduced the wrong axis, or reduced a different run, passes every shape
        assertion above and fails this one.
        """

        percentiles = (5.0, 50.0, 95.0)
        fan = engine.product_fan(run, primary_agent_id=AGENT, metric="cash_quanta", percentiles=percentiles)
        samples = engine.product_terminal(run, primary_agent_id=AGENT, metric="cash_quanta").terminal_samples

        assert fan.percentiles == percentiles
        assert fan.monthly_percentiles.shape == (HORIZON_MONTHS + 1, len(percentiles))
        assert list(fan.terminal_percentiles) == sorted(fan.terminal_percentiles), "percentiles must not decrease"
        assert min(samples) <= min(fan.terminal_percentiles)
        assert max(fan.terminal_percentiles) <= max(samples)

    def test_one_execution_answers_both_summaries(self, engine: Engine, run: CompiledRun) -> None:
        """`product_summaries` is the two of them together, not a third reduction."""

        percentiles = (5.0, 50.0, 95.0)
        both = engine.product_summaries(run, primary_agent_id=AGENT, metric="cash_quanta", percentiles=percentiles)
        assert list(both.metric_fan.terminal_percentiles) == list(
            engine.product_fan(
                run, primary_agent_id=AGENT, metric="cash_quanta", percentiles=percentiles
            ).terminal_percentiles
        )
        assert list(both.terminal_distribution.terminal_samples) == list(
            engine.product_terminal(run, primary_agent_id=AGENT, metric="cash_quanta").terminal_samples
        )
