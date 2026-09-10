"""Typed surface of the `simulator` CPython extension built from `python.rs`.

The extension has no Python source for mypy to read, so this stub is the contract; it
must be edited in lockstep with the `#[pymodule]` block in `python.rs`.
"""

from typing import Literal

from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.results import Finished, Receipt

class ProductMetrics:
    """The seven base product metric series for one population."""

    @property
    def rollout_count(self) -> int: ...
    @property
    def snapshot_count(self) -> int: ...
    @property
    def base_series(self) -> list[list[int]]:
        """One flat row-major `[snapshot][rollout]` block per `metric_names` entry."""

    @property
    def failed_month(self) -> list[int]:
        """Per-rollout failure month; `-1` for a rollout that never failed."""

    @property
    def metric_names(self) -> list[str]: ...

def simulate_product_metrics(run: CompiledRun, primary_agent_id: str) -> ProductMetrics: ...
def simulate_dense_json(run: CompiledRun) -> str: ...
def simulate_forensic_json(run: CompiledRun) -> str: ...

class HoldingPool:
    @property
    def account_id(self) -> str: ...
    @property
    def asset_id(self) -> str: ...
    @property
    def quantity_scale(self) -> int: ...
    @property
    def price(self) -> int: ...

class PublicPosition:
    @property
    def account_id(self) -> str: ...
    @property
    def asset_id(self) -> str: ...
    @property
    def lot_id(self) -> str: ...
    @property
    def purchase_month(self) -> int: ...
    @property
    def units(self) -> int: ...
    @property
    def quantity_scale(self) -> int: ...
    @property
    def book_basis(self) -> int: ...
    @property
    def price(self) -> int: ...
    @property
    def value(self) -> int: ...

class FixedCoupon:
    """Known nominal payment in currency quanta, including zero."""

    @property
    def amount(self) -> int: ...

class IndexedCoupon:
    """Rate applied to indexed principal; no future CPI or payment amount."""

    @property
    def annual_rate_ppb(self) -> int: ...

class HeldBond:
    """Owned unredeemed contract; principal is par/indexed carrying value, not sale proceeds.

    Money is currency quanta; indexed annual rates use parts per billion.
    Coupons/redemption due this month already reached observed cash. Terms may name
    future dates and fixed payments, but no future CPI or indexed payments.
    """

    @property
    def bond_id(self) -> str: ...
    @property
    def account_id(self) -> str: ...
    @property
    def issuer_jurisdiction_id(self) -> str | None: ...
    @property
    def face_value(self) -> int: ...
    @property
    def purchase_price(self) -> int: ...
    @property
    def coupon(self) -> FixedCoupon | IndexedCoupon: ...
    @property
    def coupon_period_months(self) -> int: ...
    @property
    def purchase_month(self) -> int: ...
    @property
    def maturity_month(self) -> int: ...
    @property
    def principal(self) -> int: ...

class Claim:
    """An opaque occurrence handle with copied current claim facts; not a future bill."""

    @property
    def cause_id(self) -> str: ...
    @property
    def obligation_type(self) -> str: ...
    @property
    def from_account(self) -> tuple[str, str]: ...
    @property
    def to_account(self) -> tuple[str, str]: ...
    @property
    def amount_due(self) -> int: ...
    @property
    def due_month(self) -> int: ...

class Observation:
    """Copied actor facts after scheduled events and due-claim assembly.

    Money uses input currency quanta; positions use their declared pool's scale.
    No future paths or other actors' books cross this boundary. Prior action receipts
    retain their canonical JSON representation and cover only the previous month.
    """

    @property
    def agent_id(self) -> str: ...
    @property
    def month(self) -> int: ...
    @property
    def cpi(self) -> tuple[int, int] | None:
        """Exact current/origin CPI, or None when unmodeled; never an assumed flat index."""
    @property
    def cash(self) -> int: ...
    @property
    def public_holdings(self) -> int: ...
    @property
    def accounts(self) -> list[tuple[str, int]]:
        """Declared cash account ID and available quanta, scoped to this actor."""

    @property
    def holding_pools(self) -> list[HoldingPool]: ...
    @property
    def public_positions(self) -> list[PublicPosition]: ...
    @property
    def held_bonds(self) -> list[HeldBond]: ...
    @property
    def claims(self) -> list[Claim]: ...
    @property
    def previous_receipts(self) -> list[Receipt]: ...

class Decision:
    @property
    def rollout_id(self) -> int: ...
    @property
    def observation(self) -> Observation: ...

class Action:
    """Immutable exact request; execution, not construction, checks affordability/ownership.

    Account pairs are (agent ID, account ID). Values are integer currency quanta;
    quantities are integer counts in the declared holding pool's quantity scale.
    """

    @staticmethod
    def sell(
        cause_id: str, agent_id: str, proceeds_account_id: str, asset_id: str, lots: list[tuple[str, str, int]]
    ) -> Action:
        """Lots are (holding account ID, exact lot ID, quantity counts), in sale order."""

    @staticmethod
    def buy(
        cause_id: str,
        from_account: tuple[str, str],
        holding_account_id: str,
        asset_id: str,
        lot_id: str,
        units: int,
        quantity_scale: int,
    ) -> Action: ...
    @staticmethod
    def transfer(cause_id: str, from_account: tuple[str, str], to_account: tuple[str, str], amount: int) -> Action: ...
    @staticmethod
    def pay_claim(
        request_id: int, cause_id: str, claim: Claim, from_account: tuple[str, str], amount: int
    ) -> Action: ...
    @staticmethod
    def consume(
        request_id: int,
        cause_id: str,
        component_id: str,
        from_account: tuple[str, str],
        to_account: tuple[str, str],
        amount: int,
    ) -> Action: ...

class DecisionActions:
    def __init__(self, rollout_id: int, month: int, actions: list[Action]) -> None: ...
    @property
    def rollout_id(self) -> int: ...
    @property
    def month(self) -> int: ...
    @property
    def actions(self) -> list[Action]: ...

class ActionSession:
    """One household's retained monthly action session; the caller owns the Python loop.

    Start once. Submit exactly one response for every current decision, preserving
    action order. Invalid routing/input aborts this session; a rejected action stops
    only its path, retaining successful earlier actions. Finished consumes the session.
    Close in finally if policy code raises. Supports the current immediate-cash public
    security/claim slice, not configured allocators, housing or private-equity policies.
    """

    def __init__(
        self,
        run: CompiledRun,
        actor: str,
        rollout_ids: list[int],
        *,
        capture: Literal["summary", "dense", "forensic"] = "forensic",
    ) -> None: ...
    def start(self) -> list[Decision] | Finished: ...
    def advance(self, responses: list[DecisionActions]) -> list[Decision] | Finished: ...
    def close(self) -> None: ...
