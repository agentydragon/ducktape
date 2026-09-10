"""Native allocator controls moved to the real configured Python driver.

The tax schedule below is deliberately synthetic: 20% ordinary, 10% long-term,
no deductions. Assertions pin accounting/timing, not statutory fidelity.
"""

import json
from dataclasses import replace
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityDistributionKey, SecurityKey
from finance.augur.policy.configured_allocation import validate_prepared
from finance.augur.rust.prepared import _decode, _encode
from finance.augur.rust.result import RustResult, rust_result
from finance.augur.sim import configured
from finance.augur.sim.backend import compile_run
from finance.augur.sim.configured import simulate_forensic_json
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.prepared import CompiledRun, PreparedIndexedAmount, PreparedSeries
from finance.augur.sim.scenario import (
    CashflowOnly,
    DistributionTaxSlice,
    DriftBand,
    InitialLot,
    RecurringObligation,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledTransfer,
    SecurityDistribution,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, flat, levels, scenario
from finance.augur.sim.testing.fixtures import checking

STOCK = SecurityKey(symbol="stock")
SECOND = SecurityKey(symbol="second")
TAX = Jurisdiction(
    jurisdiction_id="synthetic",
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={"single": [TaxBracket(upper="Infinity", rate=0.2)]},
    ltcg_brackets={"single": [TaxBracket(upper="Infinity", rate=0.1)]},
    standard_deduction={"single": 0},
    max_capital_loss_ordinary_offset={"single": 0},
)


def _prepared(case: Case) -> CompiledRun:
    return compile_run(
        case.scenario,
        rollout_count=case.rollout_count,
        external_series=case.external_series,
        jurisdictions={"synthetic": TAX},
        locations={},
    )


def _run(case: Case) -> RustResult:
    # Reuse the existing configured output projection; no alternate test executor.
    return rust_result(json.loads(simulate_forensic_json(_prepared(case))), case.scenario)


def _policy(
    *, assets: tuple[SecurityKey, ...] = (STOCK, SECOND), purchases: bool = False, zero_exit: bool = False
) -> TargetAllocationPolicy:
    return TargetAllocationPolicy(
        agent_id="alice",
        account_id="checking",
        source_account_ids=("brokerage",),
        sleeves=[
            SleeveTarget(asset=asset, weight=0 if zero_exit and index == 0 else 1) for index, asset in enumerate(assets)
        ],
        cash_floor=0,
        cash_ceiling=0,
        cause_id_prefix="fund",
        allow_purchases=purchases,
        rebalancing=DriftBand(tolerance=0) if zero_exit else CashflowOnly(),
    )


def _claim(month: int, amount: int, identifier: str = "spending") -> ScheduledObligation:
    return ScheduledObligation(
        month=month,
        obligation_id=identifier,
        obligation_type="cash_spend",
        agent_id="alice",
        from_account_id="checking",
        to_agent_id="world",
        to_account_id="checking",
        amount_due=amount,
    )


def _case(*, purchases: bool = False, zero_exit: bool = False, single: bool = False) -> Case:
    assets = (STOCK,) if single else (STOCK, SECOND)
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(100)), ("world", Decimal(100))),
            horizon_months=13,
            initial_lots=[
                InitialLot(
                    lot_id=f"opening-{asset.symbol}",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=asset,
                    purchase_month_index=-24,
                    quantity=100 // len(assets),
                    cost_basis=500 // len(assets),
                )
                for asset in assets
            ],
            scheduled_obligations=[_claim(0, 500), _claim(12, 50)],
            scheduled_transfers=[]
            if zero_exit
            else [
                ScheduledTransfer(
                    month=12,
                    cause_id="contribution",
                    from_agent_id="world",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=100,
                )
            ],
            target_allocation_policies=[_policy(assets=assets, purchases=purchases, zero_exit=zero_exit)],
            tax_profiles=[TaxProfile(agent_id="alice", tax_authority_agent_id="world", jurisdiction_ids=["synthetic"])],
        ),
        rollout_count=1,
        series={asset: flat(Decimal(10), rollout_count=1, horizon_months=13) for asset in assets},
    )


@pytest.mark.parametrize("purchases", [False, True])
@pytest.mark.parametrize("single", [False, True])
def test_configured_funding_preserves_tax_year_claims_and_surplus(purchases: bool, single: bool) -> None:
    result = _run(_case(purchases=purchases, single=single))
    assert result.events.tax_accruals.select("month_index", "amount_quanta").rows() == [(11, 2000)]
    assert result.events.tax_settlements.select("month_index", "amount_quanta").rows() == [(12, 2000)]
    assert result.events.lot_dispositions["proceeds_quanta"].sum() == 40_000
    assert result.events.lot_dispositions["cost_basis_consumed_quanta"].sum() == 20_000
    spending = result.events.obligation_settlements.filter(pl.col("obligation_type") == "cash_spend")
    assert spending["amount_paid_quanta"].sum() == 55_000
    ending = result.cash.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 13))
    assert ending["balance_quanta"].sum() == (0 if purchases else 3000)
    bought = result.lots.filter((pl.col("month_index") == 13) & (pl.col("purchase_month_index") == 12))
    assert bought["basis_remaining_quanta"].sum() == (3000 if purchases else 0)
    if single and purchases:
        assert bought["remaining_quantity_quanta"].to_list() == [3_000_000]
    assert result.events.rollout_failures.is_empty()


def test_zero_target_partial_raise_then_full_exit_and_later_tax_funding() -> None:
    result = _run(_case(purchases=True, zero_exit=True))
    assert result.events.lot_dispositions.select("month_index", "proceeds_quanta").rows() == [
        (0, 40_000),
        (1, 10_000),
        (12, 7500),
    ]
    stock = result.lots.filter(pl.col("lot_id") == "opening-stock").sort("month_index")
    assert stock.filter(pl.col("month_index") == 1)["remaining_quantity_quanta"].to_list() == [10_000_000]
    assert stock.filter(pl.col("month_index") == 2)["remaining_quantity_quanta"].to_list() == [0]
    assert result.events.tax_settlements.select("month_index", "amount_quanta").rows() == [(12, 2500)]
    spending = result.events.obligation_settlements.filter(pl.col("obligation_type") == "cash_spend")
    assert spending["amount_paid_quanta"].sum() == 55_000
    assert result.events.rollout_failures.is_empty()


@pytest.mark.parametrize("spending", [300, 500])
def test_group_failure_does_not_undo_prior_funding_sales(spending: int) -> None:
    case = _case(single=True)
    authored = case.scenario.model_copy(
        update={
            "horizon_months": 1,
            "scheduled_transfers": [],
            "scheduled_obligations": [_claim(0, 700, "rent"), _claim(0, spending)],
            "tax_profiles": [],
        }
    )
    result = _run(
        replace(case, scenario=authored, series={STOCK: flat(Decimal(10), rollout_count=1, horizon_months=1)})
    )
    funded = spending == 300
    assert result.events.lot_dispositions["proceeds_quanta"].sum() == (90_000 if funded else 100_000)
    assert result.events.obligation_settlements["amount_paid_quanta"].sum() == (100_000 if funded else 0)
    assert result.events.rollout_failures.is_empty() == funded
    assert result.cash["month_index"].max() == 1


def test_indexed_monthly_claims_keep_sales_and_next_year_tax_events() -> None:
    case = _case(single=True)
    authored = case.scenario.model_copy(
        update={
            "initial_cash": checking(("alice", Decimal(0)), ("world", Decimal(0))),
            "scheduled_transfers": [],
            "scheduled_obligations": [],
            "recurring_obligations": [
                RecurringObligation(
                    start_month=0,
                    obligation_id="indexed",
                    obligation_type="cash_spend",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="world",
                    to_account_id="checking",
                    amount_due=SeriesIndexedAmount(base_amount=10, series=InflationKey(), adjustment_period_months=1),
                )
            ],
        }
    )
    result = _run(
        replace(
            case,
            scenario=authored,
            series={**case.series, InflationKey(): levels([[Decimal(1)] * 12 + [Decimal(2)] * 2])},
        )
    )
    paid = result.events.obligation_settlements.filter(pl.col("obligation_type") == "cash_spend").sort("month_index")
    assert paid["amount_paid_quanta"].to_list() == [1000] * 12 + [2000]
    assert result.events.tax_accruals["amount_quanta"].to_list() == [600]
    assert result.events.tax_settlements.select("month_index", "amount_quanta").rows() == [(12, 600)]
    assert result.events.rollout_failures.is_empty()


def test_empty_buyable_pool_pays_coupon_only_after_first_purchase() -> None:
    case = _case(purchases=True, single=True)
    authored = case.scenario.model_copy(
        update={
            "horizon_months": 2,
            "initial_cash": checking(("alice", Decimal(200)), ("world", Decimal(0))),
            "initial_lots": [],
            "scheduled_transfers": [],
            "scheduled_obligations": [],
            "tax_profiles": [],
            "security_distributions": [
                SecurityDistribution(
                    agent_id="alice",
                    holding_account_id="brokerage",
                    asset=STOCK,
                    to_account_id="checking",
                    tax_character=(DistributionTaxSlice(fraction=1, issuer_jurisdiction_id=None),),
                )
            ],
        }
    )
    result = _run(
        replace(
            case,
            scenario=authored,
            series={
                STOCK: flat(Decimal(100), rollout_count=1, horizon_months=2),
                SecurityDistributionKey(symbol=STOCK.symbol): flat(Decimal(1), rollout_count=1, horizon_months=2),
            },
        )
    )
    first = result.lots.filter(pl.col("month_index") == 1)
    assert first["remaining_quantity_quanta"].to_list() == [2_000_000]
    assert first["basis_remaining_quanta"].to_list() == [20_000]
    # The second month's $2 payout is reinvested, not a first-month entitlement.
    bought = result.lots.filter((pl.col("month_index") == 2) & (pl.col("purchase_month_index") == 1))
    assert bought["basis_remaining_quanta"].to_list() == [200]


def test_fifo_across_two_policy_purchase_dates_preserves_basis_and_tax_character() -> None:
    case = _case(purchases=True, single=True)
    late = _policy(assets=(STOCK,), purchases=True)
    early = late.model_copy(update={"account_id": "early-cash", "cause_id_prefix": "early"})
    authored = case.scenario.model_copy(
        update={
            "initial_cash": [
                *checking(("alice", Decimal(0)), ("world", Decimal(300))),
                *[
                    item.model_copy(update={"account_id": account})
                    for account, item in (
                        ("early-cash", checking(("alice", Decimal(200)))[0]),
                        ("proceeds", checking(("alice", Decimal(0)))[0]),
                    )
                ],
            ],
            "initial_lots": [],
            "scheduled_obligations": [],
            "target_allocation_policies": [late, early],
            "scheduled_transfers": [
                ScheduledTransfer(
                    month=1,
                    cause_id="later",
                    from_agent_id="world",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=300,
                )
            ],
            "scheduled_asset_sales": [
                ScheduledAssetSale(
                    month=12,
                    cause_id="fifo-sale",
                    agent_id="alice",
                    source_account_id="brokerage",
                    asset=STOCK,
                    quantity=3,
                    proceeds_account_id="proceeds",
                )
            ],
        }
    )
    result = _run(
        replace(case, scenario=authored, series={STOCK: levels([[Decimal(100), Decimal(150)] + [Decimal(200)] * 12])})
    )
    sales = result.events.lot_dispositions
    assert sales.select(
        "purchase_month_index", "units_sold", "cost_basis_consumed_quanta", "proceeds_quanta"
    ).rows() == [(0, 2.0, 20_000, 40_000), (1, 1.0, 15_000, 20_000)]
    gains = result.capital_gains.filter(pl.col("month_index") == 13)
    assert dict(gains.select("classification", "gain_quanta").rows()) == {"ltcg": 20_000, "stcg": 5000}


def test_generated_purchase_namespace_is_reserved_before_execution() -> None:
    case = _case(purchases=True, single=True)
    opening = case.scenario.initial_lots[0].model_copy(update={"lot_id": "fund_buy_p0_s0_1000000"})
    authored = case.scenario.model_copy(update={"initial_lots": [opening]})
    with pytest.raises(ValueError, match="reserved allocation-purchase identity"):
        _run(replace(case, scenario=authored))
    disabled = authored.model_copy(update={"target_allocation_policies": [_policy(assets=(STOCK,), purchases=False)]})
    _run(replace(case, scenario=disabled))
    nonmatching = authored.model_copy(
        update={"initial_lots": [opening.model_copy(update={"lot_id": opening.lot_id + "x"})]}
    )
    _run(replace(case, scenario=nonmatching))


def test_missing_asset_quote_is_not_a_zero_valued_holding() -> None:
    case = _case()
    with pytest.raises(ValueError, match=r"(?i)missing|series|level block"):
        _run(replace(case, series={STOCK: case.series[STOCK]}))


@pytest.mark.parametrize(
    ("malformation", "error"),
    [
        ("sources", "source accounts must be unique"),
        ("purchase_pool", "purchase pool is not declared"),
        ("source_grid", "quantity grid disagrees"),
        ("invalid_grid", "power-of-ten quantity grid"),
        ("funding", "declared funding account"),
        ("cause", "nonempty cause"),
        ("duplicate_policy", "duplicate allocation funding account"),
        ("duplicate_sleeve", "duplicate allocation sleeve"),
        ("zero_weights", "positive target"),
        ("negative_weight", "nonnegative"),
        ("disabled_drift", "drift requires purchases"),
        ("band", "must not exceed"),
        ("period", "invalid base month or reset period"),
        ("base_month", "starts before its base month"),
        ("missing_index", "missing series"),
        ("future_index", "positive index levels"),
    ],
)
def test_imported_policy_is_rejected_before_any_world_is_constructed(
    malformation: str, error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _prepared(_case(purchases=True, single=True))
    [policy] = run.scenario._target_allocation_policies
    [sleeve] = policy.sleeves
    index = PreparedIndexedAmount(base_amount=0, series_id="inflation", base_month_index=0, adjustment_period_months=12)
    match malformation:
        case "sources":
            policy = replace(policy, source_account_ids=("brokerage", "brokerage"))
        case "purchase_pool":
            policy = replace(policy, source_account_ids=("undeclared",))
        case "source_grid":
            policy = replace(policy, sleeves=(replace(sleeve, quantity_scale=10),))
        case "invalid_grid":
            policy = replace(policy, sleeves=(replace(sleeve, quantity_scale=3),))
        case "funding":
            policy = replace(policy, account_id="undeclared")
        case "cause":
            policy = replace(policy, cause_id_prefix=" ")
        case "duplicate_sleeve":
            policy = replace(policy, sleeves=(sleeve, sleeve))
        case "zero_weights":
            policy = replace(policy, sleeves=(replace(sleeve, weight=0),))
        case "negative_weight":
            policy = replace(policy, sleeves=(replace(sleeve, weight=-1),))
        case "disabled_drift":
            policy = replace(policy, allow_purchases=False, rebalance_tolerance_ppb=0)
        case "band":
            policy = replace(policy, cash_floor=1)
        case "period":
            policy = replace(policy, cash_ceiling=replace(index, adjustment_period_months=0))
        case "base_month":
            policy = replace(policy, cash_ceiling=replace(index, base_month_index=1))
        case "missing_index" | "future_index":
            policy = replace(policy, cash_ceiling=index)
    if malformation == "future_index":
        run = replace(
            run,
            series=(*run.series, PreparedSeries(series_id="inflation", snapshots=14, values=(10**9,) * 12 + (0, 0))),
        )
    policies = (policy, policy) if malformation == "duplicate_policy" else (policy,)
    imported = _decode(_encode(replace(run, scenario=replace(run.scenario, _target_allocation_policies=policies))))

    def unexpected_world(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid configured policy reached financial world construction")

    monkeypatch.setattr(configured, "_Session", unexpected_world)
    with pytest.raises(ValueError, match=error):
        simulate_forensic_json(imported)


def test_prepared_policy_keeps_exact_integer_indices_and_disabled_purchase_scope() -> None:
    run = _prepared(_case(single=True))
    [policy] = run.scenario._target_allocation_policies
    # Policy bounds no longer cross a float transport. An exact i64 index above
    # 2**53 is valid; absent purchase destinations remain valid for sales-only rules.
    index = PreparedIndexedAmount(base_amount=0, series_id="inflation", base_month_index=0, adjustment_period_months=12)
    policy = replace(policy, source_account_ids=("unused-holdings",), cash_ceiling=index)
    run = replace(
        run,
        scenario=replace(run.scenario, _target_allocation_policies=(policy,)),
        series=(*run.series, PreparedSeries(series_id="inflation", snapshots=14, values=(2**53 + 1,) * 14)),
    )
    validate_prepared(_decode(_encode(run)))


if __name__ == "__main__":
    pytest_bazel.main()
