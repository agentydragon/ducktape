"""Typed monthly-action results; traces retain columnar events, not duplicate event objects."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, InstanceOf, JsonValue, TypeAdapter, field_serializer, field_validator, model_validator

from finance.augur.sim.actions import Action, ClaimId
from finance.augur.sim.books import (
    AccountRef,
    BondCashflowOutcome,
    Book,
    DistributionOutcome,
    JournalEntry,
    Record,
    TaxAccrual,
    TaxPaymentOutcome,
    TaxSettlementOutcome,
)
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog


class RejectedAction(Record):
    kind: Literal["RejectedAction"] = "RejectedAction"
    month: int
    action_index: int


class UnpaidClaims(Record):
    kind: Literal["UnpaidClaims"] = "UnpaidClaims"
    month: int
    claims: list[ClaimId]


type Stop = Annotated[RejectedAction | UnpaidClaims, Field(discriminator="kind")]


class PaymentRequestError(Record):
    kind: Literal[
        "UnknownClaim",
        "AlreadyPaid",
        "WrongActor",
        "SameActorRecipient",
        "UnknownAccount",
        "InvalidAmount",
        "EmptyIdentifier",
    ]


class InsufficientCash(Record):
    kind: Literal["InsufficientCash"] = "InsufficientCash"
    available: int


class UnfundedGroup(Record):
    kind: Literal["UnfundedGroup"] = "UnfundedGroup"
    available: int
    due: int


type PaymentFailure = Annotated[PaymentRequestError | InsufficientCash | UnfundedGroup, Field(discriminator="kind")]


class Paid(Record):
    kind: Literal["Paid"] = "Paid"


class PaymentRejected(Record):
    kind: Literal["Rejected"] = "Rejected"
    reason: PaymentFailure


class ClaimTarget(ClaimId):
    kind: Literal["Claim"] = "Claim"


class ConsumptionTarget(Record):
    kind: Literal["Consumption"] = "Consumption"
    component_id: str


class PaymentReceipt(Record):
    request_id: int
    target: Annotated[ClaimTarget | ConsumptionTarget, Field(discriminator="kind")]
    amount_requested: int
    outcome: Annotated[Paid | PaymentRejected, Field(discriminator="kind")]

    @property
    def amount_paid(self) -> int:
        return self.amount_requested if isinstance(self.outcome, Paid) else 0


class InvalidRequest(Record):
    kind: Literal["InvalidRequest"] = "InvalidRequest"
    detail: str


class PaymentRejection(Record):
    kind: Literal["Payment"] = "Payment"
    detail: PaymentFailure


class Executed(Record):
    kind: Literal["Executed"] = "Executed"


class Rejected(Record):
    kind: Literal["Rejected"] = "Rejected"
    reason: Annotated[InvalidRequest | PaymentRejection, Field(discriminator="kind")]


class Receipt(Record):
    month: int
    action_index: int
    action: Action
    outcome: Annotated[Executed | Rejected, Field(discriminator="kind")]


_RECEIPTS = TypeAdapter(list[Receipt])


def receipts_from_json(document: str) -> list[Receipt]:
    """One decode for the previous month's receipts at the native observation boundary."""
    return _RECEIPTS.validate_json(document)


class CashSeries(Record):
    account: AccountRef
    values: list[int]


class HoldingSeries(CashSeries):
    asset_id: str


class BondSeries(CashSeries):
    bond_id: str


class PaymentTarget(Record):
    to_account: AccountRef = Field(alias="to")
    obligation_type: str
    label: str
    is_tax_payment: bool


class Payment(Record):
    month: int
    action_index: int
    cause_id: str
    from_account: AccountRef = Field(alias="from")
    target: PaymentTarget | None
    receipt: PaymentReceipt


class UnpaidClaim(Record):
    id: ClaimId
    cause_id: str
    obligation_type: str
    from_account: AccountRef = Field(alias="from")
    to_account: AccountRef = Field(alias="to")
    amount_due: int


class Summary(Record):
    """Opening plus observed closings only. Post-stop padding is not financial data."""

    actor_id: str
    cash: list[CashSeries]
    public_holdings: list[HoldingSeries]
    bond_principal: list[BondSeries]
    payments: list[Payment]
    unpaid_claims: list[UnpaidClaim]
    tax_accruals: list[TaxAccrual]
    tax_payments: list[TaxPaymentOutcome]
    tax_settlements: list[TaxSettlementOutcome]
    ending_book: Book
    ending_mark_month: int
    last_receipts: list[Receipt]


class EventPayload(Record):
    """File/native transport only; immediately converted to the existing columnar EventLog."""

    rollout_ids: list[int]
    frames: dict[str, list[dict[str, JsonValue]]]


class Trace(Record):
    events: InstanceOf[EventLog]
    books: list[Book]
    journal: list[JournalEntry]
    bond_cashflows: list[BondCashflowOutcome]
    distributions: list[DistributionOutcome]
    receipts: list[Receipt]

    @field_validator("events", mode="before")
    @classmethod
    def decode_events(cls, value: Any) -> EventLog:
        if isinstance(value, EventLog):
            return value
        payload = EventPayload.model_validate(value)
        return EventLog.from_serialized(payload.frames, rollout_ids=payload.rollout_ids)

    @field_serializer("events")
    def serialize_events(self, events: EventLog) -> EventPayload:
        return EventPayload(
            rollout_ids=list(events.rollout_ids),
            frames={spec.name: events.frame(spec).to_dicts() for spec in EVENT_FRAME_SPECS},
        )


class Rollout(Record):
    rollout_id: int
    summary: Summary
    trace: Trace | None
    stop: Stop | None

    @model_validator(mode="after")
    def trace_belongs_to_rollout(self) -> Self:
        if self.trace is not None and self.trace.events.rollout_ids != (self.rollout_id,):
            raise ValueError("trace source identity must match its owning rollout")
        return self


class Finished(Record):
    rollouts: list[Rollout]

    @model_validator(mode="after")
    def unique_rollouts(self) -> Self:
        ids = [rollout.rollout_id for rollout in self.rollouts]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("finished batch must contain a nonempty unique selection of original rollout IDs")
        return self
