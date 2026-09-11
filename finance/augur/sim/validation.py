"""Validate resolved financial references and supplied paths before constructing books.

This boundary also serves imported CompiledRuns; source-model validation alone
cannot protect execution. Inventory-dependent sales remain checked at execution,
when prior purchases and sales have established the available units.
"""

from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, validate_currency_quantum
from finance.augur.sim.holdings import private_issuer
from finance.augur.sim.money import is_quantity_scale
from finance.augur.sim.prepared import CompiledRun, PreparedFixedAmount, PreparedIndexedCoupon, PreparedSeries
from finance.augur.sim.scenario import InterestIncome


def validate(run: CompiledRun) -> None:
    scenario = run.scenario
    if run.rollout_count <= 0 or scenario.horizon_months <= 0:
        raise ValueError("rollouts and horizon must be positive")
    if len(run.currency_code) != 3 or any(not "A" <= char <= "Z" for char in run.currency_code):
        raise ValueError("currency code must contain three uppercase ASCII letters")
    validate_currency_quantum(run.currency_quantum)
    snapshots = scenario.horizon_months + 1
    managed = {(p.owner_agent_id, p.account_id, p.asset_id) for p in scenario.tlh_portfolios}
    managed_assets = {p.asset_id for p in scenario.tlh_portfolios}
    ordinary_assets = {lot.asset_id for lot in scenario.initial_lots} | {
        p.asset_id for p in scenario.holding_pools if (p.agent_id, p.account_id, p.asset_id) not in managed
    }
    series: dict[str, PreparedSeries] = {}
    for path in run.series:
        if path.series_id in series:
            raise ValueError(f"duplicate series {path.series_id!r}")
        if path.snapshots != snapshots or len(path.values) != run.rollout_count * snapshots:
            raise ValueError(f"series {path.series_id!r} has invalid shape; expected {run.rollout_count} x {snapshots}")
        series[path.series_id] = path
        if path.series_id.startswith(("security:", "home_value:")):
            asset = path.series_id.removeprefix("security:")
            managed_only = path.series_id.startswith("security:") and asset in managed_assets - ordinary_assets
            for index, value in enumerate(path.values):
                if value < 0 or (value == 0 and not managed_only):
                    raise ValueError(f"series {path.series_id!r} has non-positive value {value} at index {index}")
        elif path.series_id.startswith("security_distribution:"):
            if any(value < 0 for value in path.values):
                raise ValueError(f"series {path.series_id!r} has a negative security distribution")

    def require_series(series_id: str) -> PreparedSeries:
        if series_id not in series:
            raise ValueError(f'missing series "{series_id}"')
        return series[series_id]

    accounts = {item.account for item in scenario.accounts}

    def require_account(account: AccountRef, context: str) -> None:
        if account not in accounts:
            raise ValueError(f"{context} references unknown account {account.agent_id}:{account.account_id}")

    for flow in (
        *scenario.scheduled_transfers,
        *scenario.recurring_transfers,
        *scenario.scheduled_property_cashflows,
        *scenario.recurring_property_cashflows,
    ):
        require_account(flow.from_account, flow.cause_id)
        require_account(flow.to_account, flow.cause_id)
        if flow.income_category is not None and flow.income_category not in scenario.income_sources:
            raise ValueError(f"{flow.cause_id!r} has undeclared income source {flow.income_category!r}")
    pools = {(p.agent_id, p.account_id, p.asset_id) for p in scenario.holding_pools}
    scales = {(p.agent_id, p.account_id, p.asset_id): p.quantity_scale for p in scenario.holding_pools}
    if len(pools) != len(scenario.holding_pools):
        raise ValueError("duplicate holding pool declaration")
    for lot in scenario.initial_lots:
        if (lot.agent_id, lot.account_id, lot.asset_id) not in pools:
            raise ValueError(f"lot {lot.lot_id!r} references no declared holding pool")
        if lot.quantity_scale != scales[lot.agent_id, lot.account_id, lot.asset_id]:
            raise ValueError(f"lot {lot.lot_id!r} has a mixed quantity scale")
    for pool in scenario.holding_pools:
        if not is_quantity_scale(pool.quantity_scale):
            raise ValueError("invalid holding pool quantity scale")
        if private_issuer(pool.asset_id) is None:
            require_series(f"security:{pool.asset_id}")
    for portfolio in scenario.tlh_portfolios:
        require_series(f"security:{portfolio.asset_id}")
    jurisdictions = {item.jurisdiction_id for item in scenario.jurisdictions}
    for bond in scenario.initial_bonds:
        if bond.issuer_jurisdiction_id is not None and bond.issuer_jurisdiction_id not in jurisdictions:
            raise ValueError(f"bond {bond.bond_id!r} has unknown issuer")
        term = bond.maturity_month_index - bond.purchase_month_index
        coupon = bond.coupon.amount if isinstance(bond.coupon, PreparedFixedAmount) else bond.coupon.annual_rate_ppb
        if (
            bond.face_value <= 0
            or bond.purchase_price != bond.face_value
            or coupon < 0
            or bond.coupon_period_months <= 0
            or term <= 0
            or term % bond.coupon_period_months
        ):
            raise ValueError(f"invalid bond terms for {bond.bond_id!r}")
        if isinstance(bond.coupon, PreparedIndexedCoupon):
            inflation = require_series("inflation")
            if max(0, bond.purchase_month_index) > scenario.horizon_months or any(v <= 0 for v in inflation.values):
                raise ValueError(f"invalid bond inflation path for {bond.bond_id!r}")
        require_account(AccountRef(agent_id=bond.agent_id, account_id=bond.account_id), f"bond {bond.bond_id!r}")
    for distribution in scenario.distributions:
        if (
            not distribution.tax_character
            or any(not 0 <= part.fraction_ppb <= MONEY_FACTOR_SCALE for part in distribution.tax_character)
            or sum(part.fraction_ppb for part in distribution.tax_character) != MONEY_FACTOR_SCALE
        ):
            raise ValueError("invalid distribution tax character split")
        for part in distribution.tax_character:
            if part.issuer_jurisdiction_id is not None and part.issuer_jurisdiction_id not in jurisdictions:
                raise ValueError("distribution has unknown issuer")
            if InterestIncome(issuer_jurisdiction_id=part.issuer_jurisdiction_id) not in scenario.income_sources:
                raise ValueError("distribution has undeclared income source")
        key = (distribution.agent_id, distribution.holding_account_id, distribution.asset_id)
        if key not in pools | managed:
            raise ValueError(f"distribution references no lots for {':'.join(key)}")
        require_account(
            AccountRef(agent_id=distribution.agent_id, account_id=distribution.to_account_id), "distribution"
        )
        require_series(f"security_distribution:{distribution.asset_id}")
    for sale in scenario._scheduled_sales:
        require_account(
            AccountRef(agent_id=sale.agent_id, account_id=sale.proceeds_account_id), f"sale {sale.cause_id!r}"
        )
        if (sale.agent_id, sale.account_id, sale.asset_id) not in pools | managed:
            raise ValueError(f"sale {sale.cause_id!r} references no holding pool")
        require_series(f"security:{sale.asset_id}")
    locations = {location.location_id for location in scenario.locations}
    for purchase in scenario._scheduled_property_purchases:
        if purchase.location_id not in locations:
            raise ValueError(f"property {purchase.property_id!r} references unknown location")
        require_account(
            AccountRef(agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id), purchase.cause_id
        )
        require_account(
            AccountRef(agent_id=purchase.seller_agent_id, account_id=purchase.seller_account_id), purchase.cause_id
        )
        principal = 0 if purchase.mortgage is None else purchase.mortgage.principal
        if (
            purchase.purchase_price <= 0
            or purchase.down_payment < 0
            or purchase.buyer_closing_cost < 0
            or not 0 <= purchase.rented_fraction_ppb <= MONEY_FACTOR_SCALE
            or not 0 <= purchase.land_value_fraction_ppb <= MONEY_FACTOR_SCALE
            or purchase.down_payment + principal != purchase.purchase_price
        ):
            raise ValueError(f"invalid property terms for {purchase.property_id!r}")
    purchases = {p.property_id: p for p in scenario._scheduled_property_purchases}
    for property_sale in scenario._property_sales:
        if property_sale.property_id not in purchases:
            raise ValueError(f"sale references unknown property {property_sale.property_id!r}")
        require_series(f"home_value:{purchases[property_sale.property_id].location_id}")

    issuers = {issuer for lot in scenario.initial_lots if (issuer := private_issuer(lot.asset_id)) is not None}
    for issuer in issuers:
        for channel, minimum, maximum in (
            ("mark", 0, (1 << 63) - 1),
            ("regime", 1, 4),
            ("event_kind", 0, 7),
            ("sale_opportunity", 0, 1),
            ("sale_capacity", 0, MONEY_FACTOR_SCALE),
            ("eligible", 0, MONEY_FACTOR_SCALE),
            ("forced_sale", 0, MONEY_FACTOR_SCALE),
            ("liquidity_blocked", 0, 1),
            ("forced_recovery", 0, (1 << 63) - 1),
            ("company_valuation", 0, (1 << 63) - 1),
        ):
            path = require_series(f"private_equity_{channel}:{issuer}")
            for index, value in enumerate(path.values):
                if not minimum <= value <= maximum:
                    raise ValueError(
                        f"private-equity issuer {issuer!r} has invalid {channel} value {value} at index {index}"
                    )
        events = require_series(f"private_equity_event_kind:{issuer}")
        opportunities = require_series(f"private_equity_sale_opportunity:{issuer}")
        if any(
            (event == 1) != (active == 1) for event, active in zip(events.values, opportunities.values, strict=True)
        ):
            raise ValueError(f"private-equity issuer {issuer!r} has invalid event/opportunity consistency")
