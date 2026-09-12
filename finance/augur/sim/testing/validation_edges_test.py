"""What a world does with facts it cannot use: an unpriceable sleeve, an oversold lot, a mark that is not a mark.

Each refusal sits where the fact is declared or lowered, so nothing carries a number it cannot
price into a month's arithmetic or into the terminal snapshot.
"""

from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode, SecurityKey, SecuritySymbol
from finance.augur.product.household import ConfiguredHousehold
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.private_equity import compile_pe_channels
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedHoldingPool, PreparedLot, PreparedSeries, _ScheduledSale
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE = "alice"
CHECKING = "checking"
ACME = "acme"
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
SCALE = quantity_scale_for_asset(VTI)
HORIZON = 2
# A per-issuer channel's level when nothing about the issuer is meant to happen.
QUIET_CHANNELS = {
    "mark": 1,
    "regime": int(PrivateEquityRegimeCode.PRIVATE_OPERATING),
    "event_kind": int(PrivateEquityEventKindCode.NONE),
    "sale_opportunity": 0,
    "sale_capacity": 1_000_000_000,
    "eligible": 1_000_000_000,
    "forced_sale": 0,
    "liquidity_blocked": 0,
    "forced_recovery": 0,
    "company_valuation": 1,
}


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def alice_holding(*series: PreparedSeries) -> World:
    """Alice's brokerage and checking accounts on a path carrying exactly `series`."""
    world = World(MarketPath(series, 0, rollout_count=1), horizon_months=HORIZON)
    for account_id in (CHECKING, "brokerage"):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=ALICE, account_id=account_id), opening_balance=0)
        )
    return world


def vti_series(*levels: float) -> tuple[PreparedSeries, ...]:
    return compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(VTI, np.asarray([levels], dtype=np.float64))], rollout_count=1, horizon_months=len(levels) - 1
        ),
        rollout_count=1,
        horizon_months=len(levels) - 1,
        currency_quantum=QUANTUM,
    )


def private_equity_series(**overrides: tuple[int, ...]) -> tuple[PreparedSeries, ...]:
    """One issuer's ten channels, flat over the horizon unless a channel states its own path."""
    return tuple(
        PreparedSeries(
            series_id=f"private_equity_{channel}:{ACME}",
            snapshots=HORIZON + 1,
            values=overrides.get(channel, (level,) * (HORIZON + 1)),
        )
        for channel, level in QUIET_CHANNELS.items()
    )


def private_equity_bundle(*, channel: str, month: int, value: float) -> PrivateEquityBundle:
    shape = (1, HORIZON + 1)
    valid = PrivateEquityBundle.from_issuer_arrays(
        ACME,
        mark_usd_per_unit=np.full(shape, 100.0, dtype=np.float64),
        regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
        event_kind_code=np.full(shape, int(PrivateEquityEventKindCode.NONE), dtype=np.int64),
        sale_opportunity_active=np.zeros(shape, dtype=np.bool_),
        sale_capacity_fraction=np.ones(shape, dtype=np.float64),
        eligible_fraction=np.ones(shape, dtype=np.float64),
        forced_sale_fraction=np.zeros(shape, dtype=np.float64),
        liquidity_blocked=np.zeros(shape, dtype=np.bool_),
        forced_recovery_cashout_usd=np.zeros(shape, dtype=np.float64),
        company_valuation_usd=np.zeros(shape, dtype=np.float64),
        rollout_count=1,
        horizon_months=HORIZON,
    )
    return PrivateEquityBundle(
        valid.frame.with_columns(
            pl.when((pl.col("rollout_index") == 0) & (pl.col("month_index") == month))
            .then(pl.lit(value, dtype=pl.Float64))
            .otherwise(pl.col(channel))
            .alias(channel)
        )
    )


@pytest.mark.parametrize(
    ("channel", "bad_value", "match"),
    [
        (
            "mark_usd_per_unit",
            -1.0,
            r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
        ),
        (
            "mark_usd_per_unit",
            float("nan"),
            r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
        ),
        (
            "forced_recovery_cashout_usd",
            -1.0,
            r"private-equity forced-recovery cashout series produced a negative value",
        ),
    ],
    ids=["pe-negative-mark", "pe-nonfinite-mark", "pe-negative-recovery"],
)
def test_private_equity_channel_lowering_refuses_values_that_are_not_amounts(
    channel: str, bad_value: float, match: str
) -> None:
    # The bad value lands in month 1, inside the executable range the channels are read over.
    with pytest.raises(ValueError, match=match):
        compile_pe_channels(
            (ACME,),
            private_equity=private_equity_bundle(channel=channel, month=1, value=bad_value),
            rollout_count=1,
            horizon_months=HORIZON,
            currency_quantum=QUANTUM,
        )


def test_a_private_equity_mark_is_required_at_the_terminal_snapshot_too() -> None:
    """The last snapshot is not simulated, but it is read: it is where terminal value comes from.

    A mark that is not a mark there would be carried into the terminal portfolio rather
    than into a month's arithmetic, which is the quieter of the two failures and so the
    one worth refusing where the lot is declared.
    """
    lot = PreparedLot(
        lot_id="acme_lot",
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=f"private_equity:{ACME}",
        purchase_month=-36,
        quantity_scale=SCALE,
        units=int(quantity_to_quanta(100.0, scale=SCALE)),
        basis=money(1000),
    )

    def holder(*series: PreparedSeries) -> World:
        world = alice_holding(*series)
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=ALICE, account_id=CHECKING, asset_id=lot.asset_id, quantity_scale=lot.quantity_scale
            )
        )
        return world

    holder(*private_equity_series()).hold(lot)
    with pytest.raises(ValueError, match=r"(?i)invalid mark value"):
        holder(*private_equity_series(mark=(1, 1, -1))).hold(lot)


def test_a_scheduled_sale_may_not_exceed_the_units_held() -> None:
    world = alice_holding(*vti_series(100.0, 100.0, 100.0))
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id="taxable", asset_id=str(VTI.symbol), quantity_scale=SCALE)
    )
    world.hold(
        PreparedLot(
            lot_id="taxable_vti",
            agent_id=ALICE,
            account_id="taxable",
            asset_id=str(VTI.symbol),
            purchase_month=-12,
            quantity_scale=SCALE,
            units=int(quantity_to_quanta(5.0, scale=SCALE)),
            basis=money(400),
        )
    )
    world.track(
        ConfiguredHousehold(
            AgentId(ALICE),
            (),
            scheduled_sales=(
                _ScheduledSale(
                    month=1,
                    cause_id="oversell",
                    agent_id=ALICE,
                    account_id="taxable",
                    asset_id=str(VTI.symbol),
                    units=int(quantity_to_quanta(6.0, scale=SCALE)),
                    proceeds_account_id=CHECKING,
                ),
            ),
        )
    )
    world.start()
    world.step()
    with pytest.raises(ValueError, match=r"(?i)exceeds the units held"):
        world.step()


@pytest.mark.parametrize("bad_price", [0.0, -100.0, float("nan")], ids=["zero", "negative", "nonfinite"])
def test_a_sleeve_price_that_is_not_a_price_is_refused(bad_price: float) -> None:
    """Zero, negative and non-finite are all refused where the series is read.

    A sleeve the engine cannot price is not a sleeve worth nothing -- valuing it at zero
    would silently under-report net worth and under-fund the band, so the declaration stops
    the run rather than carrying the number forward.
    """

    def declare() -> None:
        alice_holding(*vti_series(bad_price, bad_price, bad_price)).declare_pool(
            PreparedHoldingPool(agent_id=ALICE, account_id=CHECKING, asset_id=str(VTI.symbol), quantity_scale=SCALE)
        )

    with pytest.raises(ValueError, match=r"(?i)(non-positive value|not finite|no finite level)"):
        declare()


def test_an_admitted_sleeve_is_priced_at_the_quote_the_pool_carried() -> None:
    """The anchor for the refusal above: a real quote is admitted and is what the sleeve is worth."""
    world = alice_holding(*vti_series(100.0, 110.0, 120.0))
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id=CHECKING, asset_id=str(VTI.symbol), quantity_scale=SCALE)
    )
    assert world.public_price(ALICE, str(VTI.symbol), 1) == money(110)


if __name__ == "__main__":
    pytest_bazel.main()
