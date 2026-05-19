"""Scenario configuration — Pydantic models for the user-facing
config of a simulation run.

At spike 1, the scenario carries the agents, their initial cash
balances, a list of scheduled transfer events, and the horizon in
months. Later layers extend `Scenario` with positions (asset
holdings), liabilities (mortgages), properties, policies, the
market-bundle reference, and tax profiles per agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, PositiveInt

from augur.sim.market import MarketBundle


class Agent(BaseModel):
    """An agent in the simulation. Identified by a stable id used
    on every frame keyed by agent_id."""

    agent_id: str


class InitialAccountBalance(BaseModel):
    """Starting cash for one (agent, account) pair at month 0."""

    agent_id: str
    account_id: str
    balance_usd: float


class ScheduledTransfer(BaseModel):
    """A cash transfer between two agents scheduled at a fixed
    month. Emitted by the engine as a Transfer event at that month;
    the same amount applies to every rollout.

    `income_category` tags the transfer for downstream tax
    classification. The canonical value at spike 1 is `"ordinary"`
    (W-2-style wages for the recipient). When set, the recipient's
    `ordinary_income_ytd` increments by the transferred amount at
    apply time."""

    month: int
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: float
    income_category: str | None = None


class RecurringTransfer(BaseModel):
    """A cash transfer that fires every month within a window. The
    canonical use is a recurring paycheck (income arriving monthly)
    or recurring rent / utilities (a fixed monthly cost). The engine
    emits one Transfer event per active month per rollout — the
    same amount across rollouts at spike 1.

    `start_month` is inclusive. `end_month` is inclusive when
    supplied; when `None`, the transfer fires through the scenario's
    horizon end. The `cause_id` is reused on every emitted event row
    so a user can group_by it to see "every paycheck Alice
    received"."""

    start_month: int
    end_month: int | None = None
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: float
    income_category: str | None = None

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


class InitialLot(BaseModel):
    """A tax lot that exists at scenario start. Models pre-existing
    holdings: Alice already owns 100 units of VTI bought 24 months
    before the sim starts at $80/unit. The sim creates this lot at
    month 0 as an `AssetPurchase` event with the supplied
    `purchase_month_index` (which may be negative — purchases
    pre-dating the horizon are fine and feed into LTCG/STCG
    classification of later sales)."""

    lot_id: str
    agent_id: str
    asset_id: str
    purchase_month_index: int
    quantity: float
    cost_basis_per_unit_usd: float


class ScheduledAssetSale(BaseModel):
    """Sell a configured quantity of an asset at a fixed month. The
    sale consumes from the agent's lots of that asset in FIFO order
    by `purchase_month_index`. Proceeds = `quantity * unit_price`
    are credited to `proceeds_account_id`.

    `price_per_unit_usd` is optional: when supplied the sale uses
    that price uniformly across rollouts (useful for deterministic
    tests). When `None`, the per-rollout per-month price comes from
    the scenario's `MarketBundle` — the canonical case once L5
    market integration is in play."""

    month: int
    cause_id: str
    agent_id: str
    asset_id: str
    quantity: float
    proceeds_account_id: str
    price_per_unit_usd: float | None = None


class FloorTriggeredSalePolicy(BaseModel):
    """If the agent's cash account drops below `floor_usd`, sell
    enough of the listed assets — in `asset_preference_chain` order
    — to bring cash back up to `floor_usd + replenish_buffer_usd`.
    The engine resolves "enough" per-rollout: it walks the preference
    chain, sells the minimum quantity at the current market price
    that covers the remaining deficit. If even draining every
    preferred asset leaves a residual deficit, the rollout has run
    out of disposable wealth — L11 marks it failed.

    Spike-1 limits: one policy per agent. Sale proceeds always land
    on `account_id` (the same account whose balance is being
    monitored). Market price comes from the scenario's MarketBundle
    — direct prices on individual sales aren't supported here."""

    agent_id: str
    account_id: str
    floor_usd: float
    replenish_buffer_usd: float = 0.0
    asset_preference_chain: list[str]
    cause_id_prefix: str = "floor_triggered_sale"


class TaxProfile(BaseModel):
    """A taxed agent's tax-time configuration. At spike 1 only
    single filers are modeled; later layers add MFJ / HoH and any
    filing-status-driven branching. `jurisdiction_ids` is the
    ordered list of taxing authorities — typically
    `["federal_us", "california"]` for a CA resident.

    `tax_authority_agent_id` is the destination of tax-payment
    transfers — a bookkeeping sink, not a taxed agent itself.
    `payment_account_id` is the agent's account that the engine
    debits at year-end; `tax_authority_account_id` is the matching
    credit account on the authority side."""

    agent_id: str
    filing_status: str = "single"
    jurisdiction_ids: list[str]
    tax_authority_agent_id: str
    payment_account_id: str = "checking"
    tax_authority_account_id: str = "checking"
    prior_year_tax_usd: float = 0.0


class Scenario(BaseModel):
    """Spike-1 simulation scenario. Carries the minimum to run
    a multi-rollout simulation over a fixed horizon with both
    scheduled and recurring transfers, plus tax lots and asset
    sales."""

    agents: list[Agent]
    initial_cash: list[InitialAccountBalance]
    initial_lots: list[InitialLot] = Field(default_factory=list)
    scheduled_transfers: list[ScheduledTransfer] = Field(default_factory=list)
    recurring_transfers: list[RecurringTransfer] = Field(default_factory=list)
    scheduled_asset_sales: list[ScheduledAssetSale] = Field(default_factory=list)
    market: MarketBundle = Field(default_factory=MarketBundle)
    tax_profiles: list[TaxProfile] = Field(default_factory=list)
    floor_triggered_sale_policies: list[FloorTriggeredSalePolicy] = Field(default_factory=list)
    horizon_months: PositiveInt
