"""Product projection composition and simulation service."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

import numpy as np
import polars as pl

from augur.api.config import Config
from augur.api.scenario_set import ActorRole
from augur.api.schemas import ColumnarTable
from augur.model.exogenous import ExogenousPathModel, ExogenousSamplingRequest, anchor_sampled_series_levels
from augur.model.series import INFLATION_SERIES_ID
from augur.product.projection import (
    FundingPolicy,
    MetricFanRequest,
    MetricFanResponse,
    MetricName,
    MonthlyExpenseEvent,
    PublicSecuritySaleEvent,
    RolloutEvent,
    RolloutFailureEvent,
    RolloutOutput,
    RolloutRequest,
    RolloutResponse,
    RolloutSummary,
    ScenarioKey,
    TaxAccrualEvent,
    TaxPaymentEvent,
    TerminalMetrics,
)
from augur.sim.external_series import materialize_sampled_exogenous
from augur.sim.projections import project_net_worth
from augur.sim.run import SimulationRun
from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    LiquidityPolicy,
    RecurringObligation,
    Scenario,
    SeriesIndexedAmount,
    TaxProfile,
)
from augur.sim.simulate import simulate_with_external_series

_PRIMARY_ACCOUNT_ID = "checking"
_SPEND_SINK_AGENT_ID = "spend_sink"
_SPEND_SINK_ACCOUNT_ID = "checking"
_SPEND_OBLIGATION_ID = "monthly_spend"
_TAX_AUTHORITY_AGENT_ID = "tax_authority"
_TAX_AUTHORITY_ACCOUNT_ID = "checking"
DEFAULT_CACHE_MAX_ROLLOUTS = 25_000


@dataclass
class CachedRollout:
    exogenous_model_id: str
    seed: int
    run: SimulationRun
    rollout_index: int
    primary_agent_id: str
    asset_label_by_id: dict[str, str]
    monthly_metrics: pl.DataFrame
    terminal_metrics: TerminalMetrics
    events: tuple[RolloutEvent, ...] | None = None

    @property
    def failed(self) -> bool:
        return self.terminal_metrics.failed_month_index is not None

    def to_output(self) -> RolloutOutput:
        if self.events is None:
            self.events = _rollout_events(
                self.run,
                rollout_index=self.rollout_index,
                primary_agent_id=self.primary_agent_id,
                asset_label_by_id=self.asset_label_by_id,
            )
        return RolloutOutput(
            seed=self.seed,
            failed=self.failed,
            monthly_metrics=_columnar(self.monthly_metrics),
            terminal_metrics=self.terminal_metrics,
            events=self.events,
        )


class ProductProjectionCache:
    def __init__(self, *, max_rollouts: int = DEFAULT_CACHE_MAX_ROLLOUTS) -> None:
        if max_rollouts <= 0:
            raise ValueError("max_rollouts must be positive")
        self._max_rollouts = max_rollouts
        self._rollouts: OrderedDict[tuple[ScenarioKey, int], CachedRollout] = OrderedDict()

    def get(self, scenario: ScenarioKey, seed: int) -> CachedRollout | None:
        key = (scenario, seed)
        cached = self._rollouts.get(key)
        if cached is None:
            return None
        self._rollouts.move_to_end(key)
        return cached

    def put(self, scenario: ScenarioKey, seed: int, rollout: CachedRollout) -> None:
        key = (scenario, seed)
        self._rollouts[key] = rollout
        self._rollouts.move_to_end(key)
        while len(self._rollouts) > self._max_rollouts:
            self._rollouts.popitem(last=False)


class ProductProjectionService:
    def __init__(
        self, *, augur_config: Config, exogenous_model: ExogenousPathModel, cache: ProductProjectionCache | None = None
    ) -> None:
        self._augur_config = augur_config
        self._exogenous_model = exogenous_model
        self._cache = cache or ProductProjectionCache()

    def metric_fan(self, request: MetricFanRequest) -> MetricFanResponse:
        rollouts = self._rollouts_for_seeds(request.scenario, tuple(int(seed) for seed in request.rollout_seeds))
        exogenous_model_id = _exogenous_model_id(rollouts, fallback=request.scenario.exogenous_model_id)
        return MetricFanResponse(
            exogenous_model_id=exogenous_model_id,
            metric=request.metric,
            monthly_metric_fan=_monthly_metric_fan(
                rollouts, metric=request.metric, percentiles=tuple(float(pct) for pct in request.percentiles)
            ),
            terminal_metric_percentiles=_terminal_metric_percentiles(
                rollouts, metric=request.metric, percentiles=tuple(float(pct) for pct in request.percentiles)
            ),
            rollout_summaries=_rollout_summaries(rollouts),
            failed_count=sum(1 for rollout in rollouts if rollout.failed),
        )

    def rollout(self, request: RolloutRequest) -> RolloutResponse:
        [rollout] = self._rollouts_for_seeds(request.scenario, (int(request.seed),))
        return RolloutResponse(exogenous_model_id=rollout.exogenous_model_id, rollout=rollout.to_output())

    def _rollouts_for_seeds(self, scenario: ScenarioKey, seeds: tuple[int, ...]) -> tuple[CachedRollout, ...]:
        if scenario.exogenous_model_id != "current_exogenous_model":
            raise ValueError(f"unsupported exogenous_model_id: {scenario.exogenous_model_id!r}")

        cached_by_seed: dict[int, CachedRollout] = {}
        missing_seeds: list[int] = []
        for seed in seeds:
            cached = self._cache.get(scenario, seed)
            if cached is None:
                missing_seeds.append(seed)
            else:
                cached_by_seed[seed] = cached

        if missing_seeds:
            for seed, rollout in self._simulate_missing_rollouts(scenario, tuple(missing_seeds)):
                cached_by_seed[seed] = rollout
                self._cache.put(scenario, seed, rollout)

        return tuple(cached_by_seed[seed] for seed in seeds)

    def _simulate_missing_rollouts(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...]
    ) -> tuple[tuple[int, CachedRollout], ...]:
        primary_agent_id = _primary_agent_id(self._augur_config)
        initial_lots = _configured_portfolio_lots(self._augur_config, primary_agent_id=primary_agent_id)
        required_level_series = _required_level_series_for_product_scenario(scenario_key, initial_lots=initial_lots)
        sampled = self._exogenous_model.sample(
            ExogenousSamplingRequest(
                horizon_months=int(scenario_key.horizon_months),
                rollout_seeds=seeds,
                required_level_series=required_level_series,
            )
        )
        sampled = anchor_sampled_series_levels(sampled, self._augur_config.portfolio.level_anchors)
        scenario = _scenario_from_key(
            scenario_key, augur_config=self._augur_config, primary_agent_id=primary_agent_id, initial_lots=initial_lots
        )
        run = simulate_with_external_series(
            scenario, rollout_count=len(seeds), external_series=materialize_sampled_exogenous(sampled)
        )
        exogenous_model_id = str(sampled.metadata.get("exogenous_model_id") or scenario_key.exogenous_model_id)
        monthly_by_rollout = _monthly_metrics_by_rollout(run, primary_agent_id=primary_agent_id)
        failed_months = _failed_month_indices_by_rollout(run)
        asset_label_by_id = _public_asset_label_by_series_id(self._augur_config)
        rollouts: list[tuple[int, CachedRollout]] = []
        for rollout_index, seed in enumerate(seeds):
            monthly = _required_monthly_metrics(monthly_by_rollout, rollout_index=rollout_index)
            rollouts.append(
                (
                    seed,
                    CachedRollout(
                        exogenous_model_id=exogenous_model_id,
                        seed=seed,
                        run=run,
                        rollout_index=rollout_index,
                        primary_agent_id=primary_agent_id,
                        asset_label_by_id=asset_label_by_id,
                        monthly_metrics=monthly,
                        terminal_metrics=_terminal_metrics(
                            monthly,
                            rollout_index=rollout_index,
                            failed_month_index=_required_failed_month(failed_months, rollout_index=rollout_index),
                        ),
                    ),
                )
            )
        return tuple(rollouts)


def _scenario_from_key(
    scenario_key: ScenarioKey, *, augur_config: Config, primary_agent_id: str, initial_lots: tuple[InitialLot, ...]
) -> Scenario:
    amount_due_usd: float | SeriesIndexedAmount
    if scenario_key.spend_index == "inflation":
        amount_due_usd = SeriesIndexedAmount(
            base_amount_usd=float(scenario_key.monthly_spend_usd),
            series_id=INFLATION_SERIES_ID,
            adjustment_period_months=1,
        )
    elif scenario_key.spend_index == "none":
        amount_due_usd = float(scenario_key.monthly_spend_usd)
    else:
        raise ValueError(f"unsupported spend_index: {scenario_key.spend_index!r}")

    return Scenario(
        agents=[
            Agent(agent_id=primary_agent_id),
            Agent(agent_id=_SPEND_SINK_AGENT_ID),
            Agent(agent_id=_TAX_AUTHORITY_AGENT_ID),
        ],
        initial_lots=list(initial_lots),
        initial_cash=[
            InitialAccountBalance(
                agent_id=primary_agent_id,
                account_id=_PRIMARY_ACCOUNT_ID,
                balance_usd=float(augur_config.snapshot.cash_usd),
            ),
            InitialAccountBalance(agent_id=_SPEND_SINK_AGENT_ID, account_id=_SPEND_SINK_ACCOUNT_ID, balance_usd=0.0),
            InitialAccountBalance(
                agent_id=_TAX_AUTHORITY_AGENT_ID, account_id=_TAX_AUTHORITY_ACCOUNT_ID, balance_usd=0.0
            ),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=int(scenario_key.horizon_months) - 1,
                obligation_id=_SPEND_OBLIGATION_ID,
                obligation_type="cash_spend",
                agent_id=primary_agent_id,
                from_account_id=_PRIMARY_ACCOUNT_ID,
                to_agent_id=_SPEND_SINK_AGENT_ID,
                to_account_id=_SPEND_SINK_ACCOUNT_ID,
                amount_due_usd=amount_due_usd,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id=primary_agent_id,
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id=_TAX_AUTHORITY_AGENT_ID,
                payment_account_id=_PRIMARY_ACCOUNT_ID,
                tax_authority_account_id=_TAX_AUTHORITY_ACCOUNT_ID,
            )
        ],
        liquidity_policies=_liquidity_policies_from_funding_policy(
            scenario_key.funding_policy, primary_agent_id=primary_agent_id, initial_lots=initial_lots
        ),
        horizon_months=int(scenario_key.horizon_months),
    )


def _primary_agent_id(augur_config: Config) -> str:
    primary_agents = [agent.actor_id for agent in augur_config.agents if agent.role == ActorRole.PRIMARY_OWNER]
    if len(primary_agents) != 1:
        raise ValueError(f"expected exactly one primary owner agent; got {primary_agents}")
    return primary_agents[0]


def _configured_portfolio_lots(augur_config: Config, *, primary_agent_id: str) -> tuple[InitialLot, ...]:
    lots = augur_config.portfolio.to_initial_lots()
    unsupported_owner_ids = sorted({lot.agent_id for lot in lots if lot.agent_id != primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports public-security lots owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    return lots


def _public_asset_label_by_series_id(augur_config: Config) -> dict[str, str]:
    return {
        position.value_series_id: f"{position.label or position.symbol} ({position.symbol})"
        for position in augur_config.portfolio.public_securities
    }


def _liquidity_policies_from_funding_policy(
    funding_policy: FundingPolicy, *, primary_agent_id: str, initial_lots: tuple[InitialLot, ...]
) -> list[LiquidityPolicy]:
    asset_preference_chain = _asset_preference_chain_from_sell_order(funding_policy, initial_lots=initial_lots)
    if not asset_preference_chain:
        return []
    return [
        LiquidityPolicy(
            agent_id=primary_agent_id,
            account_id=_PRIMARY_ACCOUNT_ID,
            asset_preference_chain=asset_preference_chain,
            cash_buffer_trigger_below_usd=float(funding_policy.cash_buffer_trigger_below_usd),
            cash_buffer_sale_usd=float(funding_policy.cash_buffer_sale_usd),
            cause_id_prefix="product_funding_sale",
        )
    ]


def _asset_preference_chain_from_sell_order(
    funding_policy: FundingPolicy, *, initial_lots: tuple[InitialLot, ...]
) -> list[str]:
    asset_ids: list[str] = []
    for bucket in funding_policy.sell_order:
        if bucket == "public_securities":
            asset_ids.extend(lot.asset_id for lot in initial_lots)
        else:
            raise ValueError(f"unsupported sell_order bucket: {bucket!r}")
    return list(dict.fromkeys(asset_ids))


def _required_level_series_for_product_scenario(
    scenario_key: ScenarioKey, *, initial_lots: tuple[InitialLot, ...]
) -> frozenset[str]:
    series_ids = {lot.asset_id for lot in initial_lots}
    if scenario_key.spend_index == "inflation":
        series_ids.add(INFLATION_SERIES_ID)
    return frozenset(series_ids)


def _required_monthly_metrics(monthly_by_rollout: dict[int, pl.DataFrame], *, rollout_index: int) -> pl.DataFrame:
    monthly = monthly_by_rollout.get(rollout_index)
    if monthly is None:
        raise ValueError(f"rollout {rollout_index} produced no monthly metrics")
    return monthly


def _monthly_metrics_by_rollout(run: SimulationRun, *, primary_agent_id: str) -> dict[int, pl.DataFrame]:
    net_worth = (
        project_net_worth(run)
        .filter(pl.col("agent_id") == primary_agent_id)
        .select(
            "rollout_index",
            "month_index",
            "cash_usd",
            pl.col("liquid_asset_value_usd").alias("public_security_value_usd"),
            "liquid_net_worth_usd",
        )
        .sort("rollout_index", "month_index")
    )
    if net_worth.is_empty():
        raise ValueError(f"simulation produced no net-worth metrics for agent {primary_agent_id!r}")
    shortfall = _monthly_shortfalls_by_rollout(run)
    monthly = (
        net_worth.join(shortfall, on=["rollout_index", "month_index"], how="left")
        .with_columns(pl.col("shortfall_usd").fill_null(0.0), pl.col("liquid_net_worth_usd").alias("net_worth_usd"))
        .select(
            "rollout_index",
            "month_index",
            "cash_usd",
            "public_security_value_usd",
            "liquid_net_worth_usd",
            "net_worth_usd",
            "shortfall_usd",
        )
        .sort("rollout_index", "month_index")
    )
    return {
        int(partition["rollout_index"][0]): partition.drop("rollout_index")
        for partition in monthly.partition_by("rollout_index", maintain_order=True)
    }


def _monthly_shortfalls_by_rollout(run: SimulationRun) -> pl.DataFrame:
    settlements = run.events_log.obligation_settlements
    if settlements.is_empty():
        return pl.DataFrame(
            {"rollout_index": [], "month_index": [], "shortfall_usd": []},
            schema={"rollout_index": pl.Int64(), "month_index": pl.Int64(), "shortfall_usd": pl.Float64()},
        )
    return (
        settlements.filter(pl.col("shortfall_usd") > 0)
        .with_columns((pl.col("month_index") + 1).alias("month_index"))
        .group_by(["rollout_index", "month_index"])
        .agg(pl.col("shortfall_usd").sum())
        .sort("rollout_index", "month_index")
    )


def _terminal_metrics(monthly: pl.DataFrame, *, rollout_index: int, failed_month_index: int | None) -> TerminalMetrics:
    if monthly.is_empty():
        raise ValueError(f"rollout {rollout_index} produced no monthly metrics")
    row = monthly.tail(1).row(0, named=True)
    return TerminalMetrics(
        cash_usd=float(row["cash_usd"]),
        public_security_value_usd=float(row["public_security_value_usd"]),
        liquid_net_worth_usd=float(row["liquid_net_worth_usd"]),
        net_worth_usd=float(row["net_worth_usd"]),
        shortfall_usd=_total_shortfall(monthly),
        failed_month_index=failed_month_index,
    )


def _rollout_events(
    run: SimulationRun, *, rollout_index: int, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    events = [
        *_public_security_sale_events(
            run, rollout_index=rollout_index, primary_agent_id=primary_agent_id, asset_label_by_id=asset_label_by_id
        ),
        *_tax_accrual_events(run, rollout_index=rollout_index, primary_agent_id=primary_agent_id),
        *_tax_payment_events(run, rollout_index=rollout_index, primary_agent_id=primary_agent_id),
        *_monthly_expense_events(run, rollout_index=rollout_index, primary_agent_id=primary_agent_id),
        *_failure_events(run, rollout_index=rollout_index, primary_agent_id=primary_agent_id),
    ]
    priority = {"public_security_sale": 0, "tax_accrual": 1, "tax_payment": 2, "monthly_expense": 3, "failure": 4}
    return tuple(sorted(events, key=lambda event: (event.month_index, priority[event.kind], event.label)))


def _public_security_sale_events(
    run: SimulationRun, *, rollout_index: int, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    dispositions = run.events_log.lot_dispositions
    if dispositions.is_empty():
        return ()
    sale_rows = (
        dispositions.filter((pl.col("rollout_index") == rollout_index) & (pl.col("agent_id") == primary_agent_id))
        .group_by(["month_index", "asset_id"])
        .agg(
            pl.col("units_sold").sum(),
            pl.col("proceeds_usd").sum(),
            pl.col("cost_basis_consumed_usd").sum().alias("cost_basis_usd"),
        )
        .sort("month_index", "asset_id")
    )
    return tuple(
        PublicSecuritySaleEvent(
            month_index=int(row["month_index"]),
            label=f"Sold {asset_label_by_id.get(str(row['asset_id']), str(row['asset_id']))}",
            amount_usd=float(row["proceeds_usd"]),
            detail="Public-security sale",
            asset_id=str(row["asset_id"]),
            asset_label=asset_label_by_id.get(str(row["asset_id"])),
            units=float(row["units_sold"]),
            proceeds_usd=float(row["proceeds_usd"]),
            cost_basis_usd=float(row["cost_basis_usd"]),
        )
        for row in sale_rows.iter_rows(named=True)
    )


def _monthly_expense_events(
    run: SimulationRun, *, rollout_index: int, primary_agent_id: str
) -> tuple[RolloutEvent, ...]:
    settlements = run.events_log.obligation_settlements
    if settlements.is_empty():
        return ()
    expense_rows = settlements.filter(
        (pl.col("rollout_index") == rollout_index)
        & (pl.col("agent_id") == primary_agent_id)
        & (pl.col("obligation_type") == "cash_spend")
    ).sort("month_index", "obligation_id")
    return tuple(
        MonthlyExpenseEvent(
            month_index=int(row["month_index"]),
            label="Paid monthly expenses" if float(row["shortfall_usd"]) == 0.0 else "Monthly expenses shortfall",
            amount_usd=float(row["amount_paid_usd"]),
            detail="Required monthly spend",
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in expense_rows.iter_rows(named=True)
    )


def _tax_accrual_events(run: SimulationRun, *, rollout_index: int, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    accruals = run.events_log.tax_accruals
    if accruals.is_empty():
        return ()
    accrual_rows = accruals.filter(
        (pl.col("rollout_index") == rollout_index) & (pl.col("agent_id") == primary_agent_id)
    )
    if accrual_rows.is_empty():
        return ()
    keys = ["rollout_index", "month_index", "cause_id", "agent_id", "jurisdiction_id", "tax_year_end_month"]
    breakdown_columns = [
        *keys,
        "ordinary_income_usd",
        "ltcg_usd",
        "stcg_usd",
        "ordinary_tax_usd",
        "capital_gain_tax_usd",
        "total_tax_usd",
    ]
    breakdowns = run.events_log.tax_breakdowns
    if not breakdowns.is_empty():
        accrual_rows = accrual_rows.join(breakdowns.select(breakdown_columns), on=keys, how="left")
    else:
        accrual_rows = accrual_rows.with_columns(
            ordinary_income_usd=pl.lit(0.0),
            ltcg_usd=pl.lit(0.0),
            stcg_usd=pl.lit(0.0),
            ordinary_tax_usd=pl.col("amount_usd"),
            capital_gain_tax_usd=pl.lit(0.0),
            total_tax_usd=pl.col("amount_usd"),
        )
    accrual_rows = accrual_rows.with_columns(
        ordinary_income_usd=pl.col("ordinary_income_usd").fill_null(0.0),
        ltcg_usd=pl.col("ltcg_usd").fill_null(0.0),
        stcg_usd=pl.col("stcg_usd").fill_null(0.0),
        ordinary_tax_usd=pl.col("ordinary_tax_usd").fill_null(pl.col("amount_usd")),
        capital_gain_tax_usd=pl.col("capital_gain_tax_usd").fill_null(0.0),
        total_tax_usd=pl.col("total_tax_usd").fill_null(pl.col("amount_usd")),
    ).sort("month_index", "jurisdiction_id")
    return tuple(
        TaxAccrualEvent(
            month_index=int(row["month_index"]),
            label=f"Accrued {_tax_jurisdiction_label(str(row['jurisdiction_id']))} tax",
            amount_usd=float(row["amount_usd"]),
            detail="Year-end tax liability",
            jurisdiction_id=str(row["jurisdiction_id"]),
            tax_year_end_month=int(row["tax_year_end_month"]),
            ordinary_income_usd=float(row["ordinary_income_usd"]),
            ltcg_usd=float(row["ltcg_usd"]),
            stcg_usd=float(row["stcg_usd"]),
            ordinary_tax_usd=float(row["ordinary_tax_usd"]),
            capital_gain_tax_usd=float(row["capital_gain_tax_usd"]),
            total_tax_usd=float(row["total_tax_usd"]),
        )
        for row in accrual_rows.iter_rows(named=True)
    )


def _tax_payment_events(run: SimulationRun, *, rollout_index: int, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    settlements = run.events_log.obligation_settlements
    if settlements.is_empty():
        return ()
    tax_payment_rows = settlements.filter(
        (pl.col("rollout_index") == rollout_index)
        & (pl.col("agent_id") == primary_agent_id)
        & pl.col("obligation_type").is_in(["estimated_tax", "tax_true_up"])
    ).sort("month_index", "obligation_id")
    return tuple(
        TaxPaymentEvent(
            month_index=int(row["month_index"]),
            label=_tax_payment_label(str(row["obligation_type"]), shortfall_usd=float(row["shortfall_usd"])),
            amount_usd=float(row["amount_paid_usd"]),
            detail="Required tax payment",
            obligation_type=str(row["obligation_type"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in tax_payment_rows.iter_rows(named=True)
    )


def _tax_jurisdiction_label(jurisdiction_id: str) -> str:
    if jurisdiction_id == "federal_us":
        return "federal"
    if jurisdiction_id == "california":
        return "California"
    return jurisdiction_id.replace("_", " ")


def _tax_payment_label(obligation_type: str, *, shortfall_usd: float) -> str:
    if obligation_type == "estimated_tax":
        return "Estimated tax shortfall" if shortfall_usd > 0 else "Paid estimated taxes"
    if obligation_type == "tax_true_up":
        return "Tax true-up shortfall" if shortfall_usd > 0 else "Paid tax true-up"
    return "Tax payment shortfall" if shortfall_usd > 0 else "Paid taxes"


def _failure_events(run: SimulationRun, *, rollout_index: int, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    failures = run.events_log.rollout_failures
    if failures.is_empty():
        return ()
    failure_rows = failures.filter(
        (pl.col("rollout_index") == rollout_index) & (pl.col("agent_id") == primary_agent_id)
    )
    return tuple(
        RolloutFailureEvent(
            month_index=int(row["month_index"]),
            label="Rollout failed",
            amount_usd=float(row["shortfall_usd"]),
            detail="Required obligation could not be paid in full",
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in failure_rows.iter_rows(named=True)
    )


def _failed_month_indices_by_rollout(run: SimulationRun) -> dict[int, int | None]:
    if run.rollout_status.is_empty():
        return {}
    return {
        int(row["rollout_index"]): None if row["failed_month"] is None else int(row["failed_month"])
        for row in run.rollout_status.iter_rows(named=True)
    }


def _required_failed_month(failed_months: dict[int, int | None], *, rollout_index: int) -> int | None:
    if rollout_index not in failed_months:
        raise ValueError(f"missing rollout status for rollout {rollout_index}")
    return failed_months[rollout_index]


def _total_shortfall(monthly: pl.DataFrame) -> float:
    return float(monthly.select(pl.col("shortfall_usd").sum()).item())


def _columnar(frame: pl.DataFrame) -> ColumnarTable:
    return ColumnarTable(row_count=frame.height, columns=frame.to_dict(as_series=False))


def _exogenous_model_id(rollouts: tuple[CachedRollout, ...], *, fallback: str) -> str:
    for rollout in rollouts:
        return rollout.exogenous_model_id
    return fallback


def _monthly_metric_fan(
    rollouts: tuple[CachedRollout, ...], *, metric: MetricName, percentiles: tuple[float, ...]
) -> ColumnarTable:
    matrix = _metric_matrix(rollouts, metric=metric)
    if matrix is None:
        return _columnar(pl.DataFrame([], schema=_metric_fan_schema()))
    month_indices, values = matrix
    percentile_values = _percentile(values, percentiles, axis=0)
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    return ColumnarTable(
        row_count=int(month_indices.size * percentile_array.size),
        columns={
            "month_index": np.repeat(month_indices, percentile_array.size).tolist(),
            "percentile": np.tile(percentile_array, month_indices.size).tolist(),
            "value": percentile_values.T.reshape(-1).tolist(),
        },
    )


def _terminal_metric_percentiles(
    rollouts: tuple[CachedRollout, ...], *, metric: MetricName, percentiles: tuple[float, ...]
) -> ColumnarTable:
    values = np.asarray(
        [_terminal_metric_value(rollout.terminal_metrics, metric) for rollout in rollouts], dtype=np.float64
    )
    if values.size == 0:
        return _columnar(pl.DataFrame([], schema=_terminal_percentiles_schema()))
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    percentile_values = _percentile(values, percentiles, axis=0)
    return ColumnarTable(
        row_count=int(percentile_array.size),
        columns={"percentile": percentile_array.tolist(), "value": percentile_values.tolist()},
    )


def _metric_matrix(rollouts: tuple[CachedRollout, ...], *, metric: MetricName) -> tuple[np.ndarray, np.ndarray] | None:
    if not rollouts:
        return None
    month_indices = rollouts[0].monthly_metrics["month_index"].to_numpy().astype(np.int64, copy=False)
    values = np.empty((len(rollouts), month_indices.size), dtype=np.float64)
    for rollout_index, rollout in enumerate(rollouts):
        rollout_months = rollout.monthly_metrics["month_index"].to_numpy().astype(np.int64, copy=False)
        if rollout_months.shape != month_indices.shape or not np.array_equal(rollout_months, month_indices):
            raise ValueError("metric fan rollouts have inconsistent month indices")
        values[rollout_index] = rollout.monthly_metrics[metric].to_numpy().astype(np.float64, copy=False)
    return month_indices, values


def _percentile(values: np.ndarray, percentiles: tuple[float, ...], *, axis: int) -> np.ndarray:
    return cast(
        np.ndarray, np.percentile(values, np.asarray(percentiles, dtype=np.float64), axis=axis, method="linear")
    )


def _terminal_metric_value(terminal: TerminalMetrics, metric: MetricName) -> float:
    match metric:
        case "cash_usd":
            return terminal.cash_usd
        case "public_security_value_usd":
            return terminal.public_security_value_usd
        case "liquid_net_worth_usd":
            return terminal.liquid_net_worth_usd
        case "net_worth_usd":
            return terminal.net_worth_usd
        case "shortfall_usd":
            return terminal.shortfall_usd


def _rollout_summaries(rollouts: tuple[CachedRollout, ...]) -> tuple[RolloutSummary, ...]:
    sorted_rollouts = sorted(rollouts, key=_rollout_sort_key)
    count = len(sorted_rollouts)
    return tuple(
        RolloutSummary(
            seed=rollout.seed,
            failed=rollout.failed,
            terminal_metrics=rollout.terminal_metrics,
            sort_rank=rank,
            rank_percentile=((rank + 0.5) / count * 100) if count else 50.0,
        )
        for rank, rollout in enumerate(sorted_rollouts)
    )


def _rollout_sort_key(rollout: CachedRollout) -> tuple[bool, int, float, int]:
    terminal = rollout.terminal_metrics
    failed_month = terminal.failed_month_index if terminal.failed_month_index is not None else 10**9
    return (not rollout.failed, failed_month, terminal.net_worth_usd, rollout.seed)


def _metric_fan_schema() -> dict[str, pl.DataType]:
    return {"month_index": pl.Int64(), "percentile": pl.Float64(), "value": pl.Float64()}


def _terminal_percentiles_schema() -> dict[str, pl.DataType]:
    return {"percentile": pl.Float64(), "value": pl.Float64()}
