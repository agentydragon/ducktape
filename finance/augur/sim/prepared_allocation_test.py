"""What `validate_prepared` refuses in an imported configured allocation policy.

These are the guards on prepared input rather than on financial execution: a policy record
that survived serialization is rejected before any world is composed from it. The financial
behaviour of the same policies lives in <allocation_household_test.py>.
"""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey
from finance.augur.policy.configured_allocation import validate_prepared
from finance.augur.sim.artifacts import decode_prepared, encode_prepared
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.prepared import CompiledRun, PreparedIndexedAmount, PreparedSeries
from finance.augur.sim.scenario import (
    Agent,
    CashflowOnly,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)

STOCK = SecurityKey(symbol="stock")
TAX = Jurisdiction(
    jurisdiction_id="synthetic",
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={"single": [TaxBracket(upper="Infinity", rate=0.2)]},
    ltcg_brackets={"single": [TaxBracket(upper="Infinity", rate=0.1)]},
    standard_deduction={"single": 0},
    max_capital_loss_ordinary_offset={"single": 0},
)


def _policy(*, purchases: bool = True) -> TargetAllocationPolicy:
    return TargetAllocationPolicy(
        agent_id="alice",
        account_id="checking",
        source_account_ids=("brokerage",),
        sleeves=[SleeveTarget(asset=STOCK, weight=1)],
        cash_floor=0,
        cash_ceiling=0,
        cause_id_prefix="fund",
        allow_purchases=purchases,
        rebalancing=CashflowOnly(),
    )


def _scenario(*, purchases: bool = True) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="world")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(100))
            for agent_id in ("alice", "world")
        ],
        horizon_months=13,
        initial_lots=[
            InitialLot(
                lot_id="opening-stock",
                agent_id="alice",
                account_id="brokerage",
                asset=STOCK,
                purchase_month_index=-24,
                quantity=100,
                cost_basis=500,
            )
        ],
        target_allocation_policies=[_policy(purchases=purchases)],
        tax_profiles=[TaxProfile(agent_id="alice", tax_authority_agent_id="world", jurisdiction_ids=["synthetic"])],
    )


def _prepared(scenario: Scenario) -> CompiledRun:
    return compile_run(
        scenario,
        rollout_count=1,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(STOCK, np.full((1, 14), 10.0))], rollout_count=1, horizon_months=13
        ),
        jurisdictions={"synthetic": TAX},
        locations={},
    )


def _imported(run: CompiledRun) -> CompiledRun:
    return decode_prepared(encode_prepared(run))


def test_generated_purchase_namespace_is_reserved() -> None:
    scenario = _scenario()
    opening = scenario.initial_lots[0].model_copy(update={"lot_id": "fund_buy_p0_s0_1000000"})
    authored = scenario.model_copy(update={"initial_lots": [opening]})
    with pytest.raises(ValueError, match="reserved allocation-purchase identity"):
        validate_prepared(_prepared(authored))
    disabled = authored.model_copy(update={"target_allocation_policies": [_policy(purchases=False)]})
    validate_prepared(_prepared(disabled))
    nonmatching = authored.model_copy(
        update={"initial_lots": [opening.model_copy(update={"lot_id": opening.lot_id + "x"})]}
    )
    validate_prepared(_prepared(nonmatching))


def test_a_missing_asset_quote_is_not_a_zero_valued_holding() -> None:
    second = SecurityKey(symbol="second")
    authored = _scenario().model_copy(
        update={
            "target_allocation_policies": [
                _policy().model_copy(
                    update={"sleeves": [SleeveTarget(asset=STOCK, weight=1), SleeveTarget(asset=second, weight=1)]}
                )
            ]
        }
    )
    with pytest.raises(ValueError, match=r"(?i)missing|series|level block"):
        _prepared(authored)


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
def test_an_imported_policy_is_rejected_before_a_world_is_composed(malformation: str, error: str) -> None:
    run = _prepared(_scenario())
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
    imported = _imported(replace(run, scenario=replace(run.scenario, _target_allocation_policies=policies)))
    with pytest.raises(ValueError, match=error):
        validate_prepared(imported)


def test_a_prepared_policy_keeps_exact_integer_indices_and_disabled_purchase_scope() -> None:
    run = _prepared(_scenario(purchases=False))
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
    validate_prepared(_imported(run))


if __name__ == "__main__":
    pytest_bazel.main()
