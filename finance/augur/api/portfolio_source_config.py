"""Optional external portfolio source configuration."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal

from pydantic import Field, NonNegativeInt, PositiveFloat, model_validator

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import HoldingKind, PortfolioAccountType, PortfolioConfig
from finance.augur.api.schemas import ApiModel
from finance.augur.sim.tlh_harvest import HarvestYieldParams

_ID_PATTERN = r"^[a-z0-9][a-z0-9_\-]*$"


class PlaidBalanceField(StrEnum):
    CURRENT = "current"
    AVAILABLE = "available"


class PlaidCashSourceConfig(ApiModel):
    plaid_account_ids: tuple[str, ...] = ()
    balance_field: PlaidBalanceField = PlaidBalanceField.CURRENT


class PlaidProxyHoldingPeriodBucket(ApiModel):
    """One holding-period band used to split a Plaid SP500 proxy's aggregate into representative lots.

    Plaid reports a tax-loss-harvesting direct-indexing sleeve (e.g. Wealthfront) as a single
    aggregate security with no per-lot acquisition dates, so without buckets the proxy collapses to
    one lot at `default_holding_period_months_at_start` and liquidation tax is modeled as entirely
    long-term. These buckets carry an offline calibration (from the institution's own tax-lot
    export) of how value and basis split across holding-period bands. The live value and cost basis
    still come from Plaid at startup and are distributed across buckets using these fixed fractions,
    so the position totals always match the current Plaid snapshot.
    """

    key: str = Field(pattern=_ID_PATTERN, description="Lot-id suffix for this bucket, e.g. 'lt12'.")
    holding_period_months_at_start: NonNegativeInt = Field(
        description="Representative months held at month 0 for this bucket's lot (e.g. the MV-weighted mean age)."
    )
    market_value_fraction: float = Field(
        gt=0.0, le=1.0, description="Share of the proxy's aggregate market value in this bucket."
    )
    cost_basis_fraction: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Share of the proxy's aggregate cost basis in this bucket. Defaults to "
            "`market_value_fraction` (uniform embedded gain) when omitted; set it explicitly to "
            "capture that older buckets carry more embedded gain than freshly harvested ones."
        ),
    )


class ReducedFormTlhModel(ApiModel):
    """`tlh_model` variant `reduced_form_tlh` — a LIMITED, DELIBERATELY-APPROXIMATE ("untruthful")
    tax-loss-harvesting model. The `type` tag is the honesty signal: it does NOT simulate the
    direct-indexing sleeve's constituent stocks. The harvestable loss is a calibrated function of
    the SP500 path (`augur.sim.scenario.HarvestPolicy`, engine phase `_apply_tlh_harvest`), not a
    real below-basis amount; all `HarvestYieldParams` are `[HEURISTIC]` (first-year-1099-B anchor,
    external decay prior). The loss is honest deferral — a basis give-back at sale repays it.

    A less-fake variant (`type: representative_sleeve_tlh`, the plan's option #3 — a handful of
    index-factor + idiosyncratic-noise sleeves with REAL FIFO harvesting) would join this as a
    sibling in `TlhModelConfig`; the discriminator then tells you which fidelity is deployed.
    """

    type: Literal["reduced_form_tlh"] = "reduced_form_tlh"
    yield_params: HarvestYieldParams = Field(description="Calibrated harvest-yield curve. All params [HEURISTIC].")
    short_term_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Optional override for the share of each month's harvested loss booked as short-term. "
            "When omitted, it is seeded from the proxy's holding_period_buckets short-term (<12mo) "
            "market-value share, defaulting to 1.0 when no buckets are configured (young account, "
            "all short-term — matching the TY2025 1099-B)."
        ),
    )


# Discriminated by `type`. Single variant today; when the plan's option #3 lands it becomes
# `Annotated[ReducedFormTlhModel | RepresentativeSleeveTlhModel, Field(discriminator="type")]`.
TlhModelConfig = ReducedFormTlhModel


class PlaidSp500ProxyGroupConfig(ApiModel):
    """Map selected Plaid investment accounts into one Augur SP500 proxy position."""

    position_id: str = Field(pattern=_ID_PATTERN)
    portfolio_account_id: str = Field(pattern=_ID_PATTERN)
    owner_agent_id: str = Field(pattern=_ID_PATTERN)
    plaid_account_ids: tuple[str, ...] = Field(min_length=1)
    account_type: PortfolioAccountType = PortfolioAccountType.TAXABLE_BROKERAGE
    account_label: str | None = None
    label: str | None = None
    symbol: str = "SP500"
    security_kind: HoldingKind = HoldingKind.OTHER
    unit_value_usd: PositiveFloat = 1000.0
    default_holding_period_months_at_start: NonNegativeInt = 0
    holding_period_buckets: tuple[PlaidProxyHoldingPeriodBucket, ...] = Field(
        default=(),
        description=(
            "Optional holding-period histogram. When empty, the proxy resolves to a single "
            "aggregate lot at `default_holding_period_months_at_start`. When set, the live Plaid "
            "value/basis is distributed across these buckets so short- vs long-term tax treatment "
            "is modeled."
        ),
    )
    tlh_model: TlhModelConfig | None = Field(
        default=None,
        description=(
            "Optional tax-loss-harvesting model for this sleeve (Piece 2b), tagged by `type` "
            "(`reduced_form_tlh`). When set, the sleeve realizes calibrated monthly capital losses "
            "with a basis give-back at sale (honest deferral). When omitted, the sleeve behaves "
            "exactly as before — no harvesting."
        ),
    )

    @model_validator(mode="after")
    def _validate_holding_period_buckets(self) -> PlaidSp500ProxyGroupConfig:
        buckets = self.holding_period_buckets
        if not buckets:
            return self
        keys = [bucket.key for bucket in buckets]
        if len(set(keys)) != len(keys):
            raise ValueError(f"holding_period_buckets keys must be unique, got {keys}")
        market_value_sum = sum(bucket.market_value_fraction for bucket in buckets)
        if not math.isclose(market_value_sum, 1.0, abs_tol=1e-2):
            raise ValueError(f"holding_period_buckets market_value_fraction must sum to ~1.0, got {market_value_sum}")
        basis_specified = [bucket.cost_basis_fraction is not None for bucket in buckets]
        if any(basis_specified) and not all(basis_specified):
            raise ValueError("holding_period_buckets cost_basis_fraction must be set on all buckets or none")
        if all(basis_specified):
            basis_sum = sum(bucket.cost_basis_fraction for bucket in buckets if bucket.cost_basis_fraction is not None)
            if not math.isclose(basis_sum, 1.0, abs_tol=1e-2):
                raise ValueError(f"holding_period_buckets cost_basis_fraction must sum to ~1.0, got {basis_sum}")
        return self


class PlaidPortfolioSourceConfig(ApiModel):
    enabled: bool = False
    database_url_env: str = "AUGUR_PLAID_DATABASE_URL"
    iso_currency_code: str = "USD"
    cash: PlaidCashSourceConfig = Field(default_factory=PlaidCashSourceConfig)
    sp500_proxy_groups: tuple[PlaidSp500ProxyGroupConfig, ...] = ()

    @model_validator(mode="after")
    def _validate_enabled_source(self) -> PlaidPortfolioSourceConfig:
        if self.enabled and not self.cash.plaid_account_ids and not self.sp500_proxy_groups:
            raise ValueError("enabled Plaid portfolio source must select cash accounts or SP500 proxy groups")
        return self


class FixedPortfolioSourceConfig(ApiModel):
    """Hand-authored portfolio facts, resolved through the same source pipeline as Plaid."""

    enabled: bool = True
    snapshot: FinanceSnapshot | None = None
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)


class PortfolioSourcesConfig(ApiModel):
    fixed: FixedPortfolioSourceConfig = Field(default_factory=FixedPortfolioSourceConfig)
    plaid: PlaidPortfolioSourceConfig = Field(default_factory=PlaidPortfolioSourceConfig)
