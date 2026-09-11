"""Full payments or typed rejections; grouped funding remains distinct from ordered actions."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from finance.augur.sim import results
from finance.augur.sim.accounting import Accounting, MortgagePaymentOutcome, TransferOutcome
from finance.augur.sim.actions import ClaimId, Consume, PayClaim
from finance.augur.sim.books import AccountRef, JournalEntry, Posting, TaxPaymentOutcome, TaxSettlementOutcome
from finance.augur.sim.claims import Claim, Claims, OrdinaryDeduction, PropertyTax, TaxPayment, TaxTrueUp
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import checked_count, mul_div
from finance.augur.sim.mortgage import MortgagePayment


@dataclass(frozen=True)
class ObligationOutcome:
    month: int
    cause_id: str
    obligation_id: str
    obligation_type: str
    from_account: AccountRef
    to_account: AccountRef
    amount_due: int
    amount_paid: int
    shortfall: int
    failure_active: bool


@dataclass(frozen=True)
class Settlement:
    failed: bool
    product_shortfall: int
    obligations: list[ObligationOutcome]


def receipt(request: PayClaim | Consume, reason: results.PaymentFailure | None) -> results.PaymentReceipt:
    target = (
        results.ClaimTarget(month=request.claim.month, index=request.claim.index)
        if isinstance(request, PayClaim)
        else results.ConsumptionTarget(component_id=request.component_id)
    )
    return results.PaymentReceipt(
        request_id=request.request_id,
        target=target,
        amount_requested=request.amount,
        outcome=results.Paid() if reason is None else results.PaymentRejected(reason=reason),
    )


def prepare(
    accounting: Accounting, month: int, claims: Claims, actor: str, request: PayClaim | Consume
) -> Claim | results.PaymentRequestError:
    if request.from_account.agent_id != actor:
        return results.PaymentRequestError(kind="WrongActor")
    if not request.cause_id:
        return results.PaymentRequestError(kind="EmptyIdentifier")
    if isinstance(request, PayClaim):
        if (
            request.claim.month != month
            or request.claim.month != claims.month
            or not 0 <= request.claim.index < len(claims.entries)
        ):
            return results.PaymentRequestError(kind="UnknownClaim")
        claim = claims.entries[request.claim.index]
        if claim.from_account.agent_id != actor:
            return results.PaymentRequestError(kind="WrongActor")
        if claim.paid:
            return results.PaymentRequestError(kind="AlreadyPaid")
        if request.amount != claim.amount_due:
            return results.PaymentRequestError(kind="InvalidAmount")
    else:
        if request.to_account.agent_id == actor:
            return results.PaymentRequestError(kind="SameActorRecipient")
        if request.amount <= 0:
            return results.PaymentRequestError(kind="InvalidAmount")
        if not request.component_id:
            return results.PaymentRequestError(kind="EmptyIdentifier")
        claim = Claim(request.cause_id, "cash_spend", request.from_account, request.to_account, request.amount, None)
    if request.from_account not in accounting.declared or claim.to_account not in accounting.declared:
        return results.PaymentRequestError(kind="UnknownAccount")
    return claim


def execute(
    accounting: Accounting, month: int, claims: Claims, actor: str, request: PayClaim | Consume
) -> results.PaymentReceipt:
    claim = prepare(accounting, month, claims, actor, request)
    if isinstance(claim, results.PaymentRequestError):
        return receipt(request, claim)
    available = accounting.ledger.balance(request.from_account)
    if available < request.amount:
        return receipt(request, results.InsufficientCash(available=available))
    post_payment(accounting, month, claim, request.from_account, request.cause_id, request.amount)
    if isinstance(request, PayClaim):
        claim.paid = True
    return receipt(request, None)


def post_payment(accounting: Accounting, month: int, claim: Claim, source: AccountRef, cause: str, amount: int) -> None:
    tax = deepcopy(accounting.tax)
    liabilities = list(accounting.tax_liabilities)
    effect = claim.effect
    postings = [
        Posting(account=source, amount=checked_count(-amount, "money negation")),
        Posting(account=claim.to_account, amount=amount),
    ]
    entries = []
    tax_settlement = None
    mortgage_payment = None
    if isinstance(effect, OrdinaryDeduction):
        tax.income.deduct_from_ordinary(
            source.agent_id, mul_div(amount, effect.fraction, MONEY_FACTOR_SCALE, "ordinary deduction")
        )
    elif isinstance(effect, PropertyTax):
        tax.property_tax(effect.owner, amount, effect.rented_fraction)
    elif isinstance(effect, TaxPayment | TaxTrueUp):
        profile = effect.profile
        if amount:
            postings.extend(
                [
                    Posting(
                        account=AccountRef(agent_id=profile.agent_id, account_id="asset:tax-prepayments"), amount=amount
                    ),
                    Posting(
                        account=AccountRef(agent_id=profile.tax_authority_agent_id, account_id="income:tax-payments"),
                        amount=checked_count(-amount, "money negation"),
                    ),
                ]
            )
            entries.append(JournalEntry(month=month, cause_id=cause, postings=postings))
        if isinstance(effect, TaxTrueUp):
            matching = [
                index
                for index, liability in enumerate(liabilities)
                if liability.active
                and liability.agent_id == profile.agent_id
                and liability.tax_year_end_month == effect.year_end_month
            ]
            total = 0
            settlement_postings = []
            for index in matching:
                liability = liabilities[index]
                total = checked_count(total + liability.amount_owed, "money addition")
                if liability.amount_owed:
                    settlement_postings.append(
                        Posting(
                            account=AccountRef(
                                agent_id=liability.agent_id, account_id=f"liability:tax:{liability.jurisdiction_id}"
                            ),
                            amount=liability.amount_owed,
                        )
                    )
                liabilities[index] = liability.model_copy(update={"amount_owed": 0})
            settlement_cause = f"{profile.agent_id}_tax_settlement_y{(effect.year_end_month - 11) // 12}"
            if total:
                settlement_postings.append(
                    Posting(
                        account=AccountRef(agent_id=profile.agent_id, account_id="asset:tax-prepayments"),
                        amount=checked_count(-total, "money negation"),
                    )
                )
                entries.append(JournalEntry(month=month, cause_id=settlement_cause, postings=settlement_postings))
            tax_settlement = TaxSettlementOutcome(
                month=month,
                cause_id=settlement_cause,
                agent_id=profile.agent_id,
                tax_year_end_month=effect.year_end_month,
                amount=total,
            )
    elif isinstance(effect, MortgagePayment):
        terms = effect.terms
        mortgage_liability = AccountRef(
            agent_id=terms.borrower.agent_id, account_id=f"liability:mortgage:{terms.liability_id}"
        )
        if effect.principal > checked_count(-accounting.ledger.balance(mortgage_liability), "money negation"):
            raise ValueError("installment exceeds ledger principal")
        postings.extend(
            [
                Posting(account=mortgage_liability, amount=effect.principal),
                Posting(
                    account=AccountRef(
                        agent_id=terms.borrower.agent_id, account_id=f"expense:mortgage-interest:{terms.liability_id}"
                    ),
                    amount=effect.interest,
                ),
                Posting(
                    account=AccountRef(
                        agent_id=terms.lender.agent_id, account_id=f"asset:mortgage-receivable:{terms.liability_id}"
                    ),
                    amount=checked_count(-effect.principal, "money negation"),
                ),
                Posting(
                    account=AccountRef(
                        agent_id=terms.lender.agent_id, account_id=f"income:mortgage-interest:{terms.liability_id}"
                    ),
                    amount=checked_count(-effect.interest, "money negation"),
                ),
            ]
        )
        if terms.borrower.agent_id in tax.years:
            year = tax.years[terms.borrower.agent_id]
            year.rental_interest_deduction = checked_count(
                year.rental_interest_deduction + effect.rental_interest, "money addition"
            )
        mortgage_payment = MortgagePaymentOutcome(
            month,
            cause,
            terms.liability_id,
            terms.borrower.agent_id,
            terms.lender.agent_id,
            terms.property_id,
            source.account_id,
            terms.lender.account_id,
            effect.interest,
            effect.principal,
            amount,
        )
    if not isinstance(effect, TaxPayment | TaxTrueUp):
        entries.append(JournalEntry(month=month, cause_id=cause, postings=postings))
    accounting.apply_entries(entries)
    accounting.tax = tax
    accounting.tax_liabilities = liabilities
    if tax_settlement is not None:
        accounting.tax_settlements.append(tax_settlement)
    if mortgage_payment is not None:
        accounting.mortgage_payments.append(mortgage_payment)
    if isinstance(effect, TaxPayment | TaxTrueUp):
        accounting.tax_payments.append(
            TaxPaymentOutcome(
                month=month,
                cause_id=cause,
                agent_id=source.agent_id,
                obligation_type=claim.obligation_type,
                amount_due=amount,
                amount_paid=amount,
                shortfall=0,
            )
        )
    if amount > 0 and accounting.capture != "summary":
        accounting.transfers.append(TransferOutcome(month, cause, source, claim.to_account, amount, None))


def settle_grouped(accounting: Accounting, claims: Claims, product_actor: str | None) -> Settlement:
    requests = [
        PayClaim(
            request_id=index + 1,
            cause_id=claim.cause_id,
            claim=ClaimId(month=claims.month, index=index),
            from_account=claim.from_account,
            amount=claim.amount_due,
        )
        for index, claim in enumerate(claims.entries)
        if not claim.paid
    ]
    due: defaultdict[AccountRef, int] = defaultdict(int)
    for request in requests:
        due[request.from_account] = checked_count(due[request.from_account] + request.amount, "money addition")
    rejections = {
        source: results.UnfundedGroup(available=accounting.ledger.balance(source), due=amount)
        for source, amount in due.items()
        if accounting.ledger.balance(source) < amount
    }
    failed = False
    shortfall = 0
    obligations = []
    for request in requests:
        outcome = (
            receipt(request, rejections[request.from_account])
            if request.from_account in rejections
            else execute(accounting, claims.month, claims, request.from_account.agent_id, request)
        )
        claim = claims.entries[request.claim.index]
        gap = checked_count(outcome.amount_requested - outcome.amount_paid, "money subtraction")
        rejected = isinstance(outcome.outcome, results.PaymentRejected)
        failed |= rejected
        if rejected and isinstance(claim.effect, TaxPayment | TaxTrueUp):
            accounting.tax_payments.append(
                TaxPaymentOutcome(
                    month=claims.month,
                    cause_id=request.cause_id,
                    agent_id=request.from_account.agent_id,
                    obligation_type=claim.obligation_type,
                    amount_due=request.amount,
                    amount_paid=0,
                    shortfall=gap,
                )
            )
        obligations.append(
            ObligationOutcome(
                claims.month,
                request.cause_id,
                request.cause_id,
                claim.obligation_type,
                request.from_account,
                claim.to_account,
                request.amount,
                outcome.amount_paid,
                gap,
                rejected,
            )
        )
        if request.from_account.agent_id == product_actor:
            shortfall = checked_count(shortfall + gap, "money addition")
    return Settlement(failed, shortfall, obligations)
