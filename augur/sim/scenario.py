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
    the same amount applies to every rollout."""

    month: int
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: float


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

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


class Scenario(BaseModel):
    """Spike-1 simulation scenario. Carries the minimum to run
    a multi-rollout simulation over a fixed horizon with both
    scheduled and recurring transfers."""

    agents: list[Agent]
    initial_cash: list[InitialAccountBalance]
    scheduled_transfers: list[ScheduledTransfer] = Field(default_factory=list)
    recurring_transfers: list[RecurringTransfer] = Field(default_factory=list)
    horizon_months: PositiveInt
