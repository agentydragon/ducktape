"""Private financial-world codecs; no policies or session lifecycle cross this boundary."""

from typing import Annotated

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter

from finance.augur.sim import results
from finance.augur.sim.books import BondCashflowOutcome, Book, DistributionOutcome, JournalEntry, MortgageState, Record
from finance.augur.sim.events import EventLog
from finance.augur.sim.observations import TlhPortfolioObservation


class ComponentEffects(Record):
    observation: TlhPortfolioObservation
    cash_account_id: str | None
    cash_amount: int
    short_term_gain: int
    long_term_gain: int
    interest: tuple[JsonValue, ...] = ()


MARKS = TypeAdapter(list[TlhPortfolioObservation])
OUTCOME: TypeAdapter[results.Executed | results.Rejected] = TypeAdapter(
    Annotated[results.Executed | results.Rejected, Field(discriminator="kind")]
)
UNPAID = TypeAdapter(list[results.UnpaidClaim])


class MortgageOrigination(Record):
    liability_id: str
    monthly_payment: int


class MortgagePayoff(Record):
    liability_id: str
    principal: int


class MortgageInstallment(Record):
    liability_id: str
    interest: int
    principal: int
    rental_interest: int


class MortgageInterest(Record):
    """Paid-interest facts for native tax assessment, before the Python year reset."""

    liability_id: str
    owner_interest_paid_ytd: int
    origination_principal: int


class MortgageOpening(Record):
    originated: list[str]
    paid_off: list[str]


MORTGAGE_ORIGINATIONS = TypeAdapter(list[MortgageOrigination])
MORTGAGE_PAYOFFS = TypeAdapter(list[MortgagePayoff])
MORTGAGE_INSTALLMENTS = TypeAdapter(list[MortgageInstallment])
MORTGAGE_INTEREST = TypeAdapter(list[MortgageInterest])
MORTGAGE_SNAPSHOTS = TypeAdapter(list[MortgageState])
PAID_MORTGAGES = TypeAdapter(list[str])


class Settlement(Record):
    failed: bool
    product_shortfall: int


class FinancialSummary(results.Summary):
    """The ledger supplies financial facts; Python attaches its own attempted prefix."""

    last_receipts: list[results.Receipt] = Field(default_factory=list)


class FinancialOutput(Record):
    """Typed trace projection; retain other legacy file fields only for configured export."""

    model_config = ConfigDict(extra="allow")

    rollout_id: int
    months: list[Book]
    journal: list[JournalEntry]
    bond_cashflows: list[BondCashflowOutcome]
    distributions: list[DistributionOutcome]
    failed_month: int | None


class ConfiguredSummary(Record):
    """Legacy terminal export retained at the private file boundary."""

    model_config = ConfigDict(extra="allow")
    failed_month: int | None


class WorldResult(Record):
    rollout_id: int
    summary: FinancialSummary | None
    financial: FinancialOutput | None
    event_frames: dict[str, list[dict[str, JsonValue]]] | None
    configured_summary: ConfiguredSummary | None
    product_metrics: list[tuple[int, int, int, int, int, int, int]]

    def rollout(
        self, receipts: list[results.Receipt], last_receipts: list[results.Receipt], stop: results.Stop | None
    ) -> results.Rollout:
        if self.summary is None:
            raise RuntimeError("actor capture requires a financial summary")
        trace = None
        if self.financial is not None:
            if self.event_frames is None:
                raise RuntimeError("detailed capture requires financial event frames")
            trace = results.Trace(
                events=EventLog.from_serialized(self.event_frames, rollout_ids=[self.rollout_id]),
                books=self.financial.months,
                journal=self.financial.journal,
                bond_cashflows=self.financial.bond_cashflows,
                distributions=self.financial.distributions,
                receipts=receipts,
            )
        return results.Rollout(
            rollout_id=self.rollout_id,
            summary=results.Summary(
                actor_id=self.summary.actor_id,
                cash=self.summary.cash,
                public_holdings=self.summary.public_holdings,
                bond_principal=self.summary.bond_principal,
                payments=self.summary.payments,
                unpaid_claims=self.summary.unpaid_claims,
                tax_accruals=self.summary.tax_accruals,
                tax_payments=self.summary.tax_payments,
                tax_settlements=self.summary.tax_settlements,
                ending_book=self.summary.ending_book,
                ending_mark_month=self.summary.ending_mark_month,
                last_receipts=last_receipts,
            ),
            trace=trace,
            stop=stop,
        )
