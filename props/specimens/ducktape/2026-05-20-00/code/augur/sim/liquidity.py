"""Liquidity policy planning.

Policies see hard due-now cash demands plus current state and emit
sale dispositions. They do not settle obligations or decide whether
an unpaid obligation is allowed to remain unpaid.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.frames import FrameSpec
from augur.sim.events import EVENT_FRAMES
from augur.sim.market import MarketContext
from augur.sim.scenario import LiquidityPolicy
from augur.sim.state import StateCrossSection

LIQUIDITY_ATTEMPTS_BY_ACCOUNT = FrameSpec(
    "_liquidity_attempts_by_account",
    pl.Schema(
        {
            "rollout_index": pl.Int64(),
            "agent_id": pl.Utf8(),
            "from_account_id": pl.Utf8(),
            "_attempted_funding_sources": pl.Utf8(),
        }
    ),
)


@dataclass(frozen=True)
class LiquidityPlan:
    """Sale decisions plus account-level diagnostics for settlement."""

    lot_dispositions: pl.DataFrame
    attempted_sources_by_account: pl.DataFrame


@dataclass(frozen=True)
class _PolicyAssetResult:
    """Output of consuming one asset within a policy."""

    dispositions: pl.DataFrame | None
    remaining_deficit: pl.DataFrame


def plan_liquidity(
    *,
    state: StateCrossSection,
    policies: list[LiquidityPolicy],
    hard_demands: pl.DataFrame,
    market: MarketContext,
    month: int,
) -> LiquidityPlan:
    """Emit liquidity-sale dispositions for active rollouts.

    A policy first covers hard demands for its account, then applies
    its optional cash-buffer rule against the post-demand cash view.
    Buffer shortfalls are just missed discretionary targets; only the
    later settlement of hard demands can fail a rollout.
    """

    if not policies:
        return LiquidityPlan(
            lot_dispositions=EVENT_FRAMES.lot_dispositions.empty(),
            attempted_sources_by_account=LIQUIDITY_ATTEMPTS_BY_ACCOUNT.empty(),
        )
    _validate_unique_policy_accounts(policies)
    active_rollouts = state.rollout_status.filter(pl.col("status") == "active").select("rollout_index")
    if active_rollouts.is_empty():
        return LiquidityPlan(
            lot_dispositions=EVENT_FRAMES.lot_dispositions.empty(),
            attempted_sources_by_account=LIQUIDITY_ATTEMPTS_BY_ACCOUNT.empty(),
        )

    demand_by_account = _hard_demand_by_account(hard_demands)
    prices = market.prices_at(month)
    disposition_blocks: list[pl.DataFrame] = []
    attempt_blocks: list[pl.DataFrame] = []
    planning_state = state
    for policy in policies:
        targets = _target_sales_for_policy(
            state=planning_state, policy=policy, demand_by_account=demand_by_account, active_rollouts=active_rollouts
        )
        attempts = _attempts_for_policy(policy, targets)
        if not attempts.is_empty():
            attempt_blocks.append(attempts)
        if targets.filter(pl.col("_remaining_deficit_usd") > 0).is_empty():
            continue
        policy_dispositions = _dispositions_for_policy(
            state=planning_state, policy=policy, prices=prices, targets=targets, month=month
        )
        if not policy_dispositions.is_empty():
            disposition_blocks.append(policy_dispositions)
            planning_state = _state_after_lot_dispositions(planning_state, policy_dispositions)

    return LiquidityPlan(
        lot_dispositions=EVENT_FRAMES.lot_dispositions.concat(disposition_blocks),
        attempted_sources_by_account=LIQUIDITY_ATTEMPTS_BY_ACCOUNT.concat(attempt_blocks),
    )


def _validate_unique_policy_accounts(policies: list[LiquidityPolicy]) -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for policy in policies:
        key = (policy.agent_id, policy.account_id)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        duplicate_list = ", ".join(f"{agent_id}/{account_id}" for agent_id, account_id in sorted(duplicates))
        raise ValueError(f"Duplicate liquidity policies for account(s): {duplicate_list}")


def _hard_demand_by_account(hard_demands: pl.DataFrame) -> pl.DataFrame:
    if hard_demands.is_empty():
        return pl.Schema(
            {
                "rollout_index": pl.Int64(),
                "agent_id": pl.Utf8(),
                "from_account_id": pl.Utf8(),
                "_hard_demand_usd": pl.Float64(),
            }
        ).to_frame()
    return hard_demands.group_by(["rollout_index", "agent_id", "from_account_id"]).agg(
        pl.col("amount_due_usd").sum().alias("_hard_demand_usd")
    )


def _target_sales_for_policy(
    *, state: StateCrossSection, policy: LiquidityPolicy, demand_by_account: pl.DataFrame, active_rollouts: pl.DataFrame
) -> pl.DataFrame:
    cash = state.cash_balances.filter(
        (pl.col("agent_id") == policy.agent_id) & (pl.col("account_id") == policy.account_id)
    ).select("rollout_index", pl.col("balance_usd").alias("_cash_balance_usd"))
    hard_demand = demand_by_account.filter(
        (pl.col("agent_id") == policy.agent_id) & (pl.col("from_account_id") == policy.account_id)
    ).select("rollout_index", "_hard_demand_usd")
    targets = (
        active_rollouts.join(cash, on="rollout_index", how="left")
        .join(hard_demand, on="rollout_index", how="left")
        .with_columns(pl.col("_cash_balance_usd").fill_null(0.0), pl.col("_hard_demand_usd").fill_null(0.0))
        .with_columns(
            _required_sale_usd=pl.max_horizontal(0.0, pl.col("_hard_demand_usd") - pl.col("_cash_balance_usd"))
        )
        .with_columns(
            _post_required_cash_usd=pl.col("_cash_balance_usd")
            + pl.col("_required_sale_usd")
            - pl.col("_hard_demand_usd")
        )
    )
    return targets.with_columns(_buffer_sale_usd=_buffer_sale_expr(policy)).with_columns(
        _remaining_deficit_usd=pl.col("_required_sale_usd") + pl.col("_buffer_sale_usd")
    )


def _buffer_sale_expr(policy: LiquidityPolicy) -> pl.Expr:
    if policy.cash_buffer_sale_usd <= 0:
        return pl.lit(0.0, dtype=pl.Float64())
    post_required_cash = pl.col("_post_required_cash_usd")
    return (
        pl.when(post_required_cash < policy.cash_buffer_trigger_below_usd)
        .then(pl.lit(policy.cash_buffer_sale_usd, dtype=pl.Float64()))
        .otherwise(pl.lit(0.0, dtype=pl.Float64()))
    )


def _attempts_for_policy(policy: LiquidityPolicy, targets: pl.DataFrame) -> pl.DataFrame:
    return (
        targets.filter((pl.col("_hard_demand_usd") > 0) | (pl.col("_remaining_deficit_usd") > 0))
        .with_columns(
            pl.lit(policy.agent_id, dtype=pl.Utf8()).alias("agent_id"),
            pl.lit(policy.account_id, dtype=pl.Utf8()).alias("from_account_id"),
            pl.lit(",".join(policy.asset_preference_chain), dtype=pl.Utf8()).alias("_attempted_funding_sources"),
        )
        .pipe(LIQUIDITY_ATTEMPTS_BY_ACCOUNT.normalize)
    )


def _dispositions_for_policy(
    *, state: StateCrossSection, policy: LiquidityPolicy, prices: pl.DataFrame, targets: pl.DataFrame, month: int
) -> pl.DataFrame:
    deficit = (
        targets.filter(pl.col("_remaining_deficit_usd") > 0)
        .select("rollout_index", "_remaining_deficit_usd")
        .sort("rollout_index")
    )
    if deficit.is_empty():
        return EVENT_FRAMES.lot_dispositions.empty()
    blocks: list[pl.DataFrame] = []
    for asset_id in policy.asset_preference_chain:
        if deficit.is_empty():
            break
        result = _consume_asset_for_policy(
            state=state,
            policy=policy,
            asset_id=asset_id,
            prices=prices,
            deficit=deficit,
            cause_id=f"{policy.cause_id_prefix}_m{month}_{asset_id}",
            month=month,
        )
        if result.dispositions is not None and not result.dispositions.is_empty():
            blocks.append(result.dispositions)
        deficit = result.remaining_deficit
    return EVENT_FRAMES.lot_dispositions.concat(blocks)


def _consume_asset_for_policy(
    *,
    state: StateCrossSection,
    policy: LiquidityPolicy,
    asset_id: str,
    prices: pl.DataFrame,
    deficit: pl.DataFrame,
    cause_id: str,
    month: int,
) -> _PolicyAssetResult:
    asset_price = prices.filter(pl.col("asset_id") == asset_id).select(
        "rollout_index", pl.col("price_per_unit_usd").alias("_unit_price")
    )
    lots = (
        state.asset_lots.filter(
            (pl.col("agent_id") == policy.agent_id)
            & (pl.col("asset_id") == asset_id)
            & (pl.col("remaining_quantity") > 0)
        )
        .join(asset_price, on="rollout_index", how="left")
        .join(deficit, on="rollout_index", how="inner")
        .filter(pl.col("_unit_price").is_not_null() & (pl.col("_unit_price") > 0))
    )
    if lots.is_empty():
        return _PolicyAssetResult(dispositions=None, remaining_deficit=deficit)
    ordered = lots.sort(["rollout_index", "purchase_month_index", "lot_id"])
    with_cum = ordered.with_columns(
        _prev_cum_dollars=(
            (pl.col("remaining_quantity") * pl.col("_unit_price")).cum_sum().over("rollout_index")
            - (pl.col("remaining_quantity") * pl.col("_unit_price"))
        )
    )
    sized = with_cum.with_columns(
        _dollars_from_lot=pl.min_horizontal(
            pl.col("remaining_quantity") * pl.col("_unit_price"),
            pl.max_horizontal(pl.lit(0.0), pl.col("_remaining_deficit_usd") - pl.col("_prev_cum_dollars")),
        )
    )
    consumed = sized.filter(pl.col("_dollars_from_lot") > 0).with_columns(
        _units_from_lot=pl.col("_dollars_from_lot") / pl.col("_unit_price")
    )
    if consumed.is_empty():
        return _PolicyAssetResult(dispositions=None, remaining_deficit=deficit)
    dispositions = consumed.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.col("_units_from_lot").alias("units_sold"),
        (pl.col("_units_from_lot") * pl.col("cost_basis_per_unit_usd")).alias("cost_basis_consumed_usd"),
        pl.col("_dollars_from_lot").alias("proceeds_usd"),
        pl.lit(policy.account_id, dtype=pl.Utf8()).alias("proceeds_account_id"),
    ).pipe(EVENT_FRAMES.lot_dispositions.normalize)
    realized_per_rollout = consumed.group_by("rollout_index").agg(
        pl.col("_dollars_from_lot").sum().alias("_realized_usd")
    )
    new_deficit = (
        deficit.join(realized_per_rollout, on="rollout_index", how="left")
        .with_columns(_remaining_deficit_usd=pl.col("_remaining_deficit_usd") - pl.col("_realized_usd").fill_null(0.0))
        .filter(pl.col("_remaining_deficit_usd") > 0)
        .select("rollout_index", "_remaining_deficit_usd")
    )
    return _PolicyAssetResult(dispositions=dispositions, remaining_deficit=new_deficit)


def _state_after_lot_dispositions(state: StateCrossSection, dispositions: pl.DataFrame) -> StateCrossSection:
    if dispositions.is_empty():
        return state
    deltas = dispositions.group_by(["rollout_index", "lot_id"]).agg(pl.col("units_sold").sum().alias("_units_sold"))
    return StateCrossSection(
        cash_balances=state.cash_balances,
        asset_lots=state.asset_lots.join(deltas, on=["rollout_index", "lot_id"], how="left")
        .with_columns(remaining_quantity=pl.col("remaining_quantity") - pl.col("_units_sold").fill_null(0.0))
        .drop("_units_sold"),
        ordinary_income_ytd=state.ordinary_income_ytd,
        capital_gains_ytd=state.capital_gains_ytd,
        tax_liabilities=state.tax_liabilities,
        property_state=state.property_state,
        property_stakes=state.property_stakes,
        liabilities=state.liabilities,
        rollout_status=state.rollout_status,
    )
