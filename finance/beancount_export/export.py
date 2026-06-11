"""Render classified budget transactions as a Beancount ledger.

This module is a *pure* projection: it turns already-classified transactions
(one bucket per txn, produced by augur's budget SQL read model) into
deterministic Beancount text. It does no I/O — fetching rows from the Plaid
mirror and committing the result to a git repo live in the exporter runner.

Serialization is delegated to the ``beancount`` library: entries are built as
``beancount.core.data`` directives and rendered with
``beancount.parser.printer``, so string escaping, number formatting and metadata
typing are handled by beancount rather than hand-rolled.

Double-entry handles signs, reversals and transfers for free: each Plaid txn
becomes one balanced two-posting entry. Plaid signs outflows positive and
inflows negative, so the bucket's *contra* leg takes ``+amount`` and the
transaction's own *funding* account (a Plaid account) takes ``-amount``. An
expense (amount > 0) thus debits Expenses and credits the asset/liability; an
income deposit (amount < 0) does the reverse. The same rule covers credit-card
charges (a Liabilities funding leg) without special-casing.

Output is byte-stable for a fixed input (entries ordered by ``(date,
transaction_id)``, accounts opened once at the earliest date, amounts quantized
to cents) so re-running the exporter produces no spurious git commits.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from beancount.core import data
from beancount.core.amount import Amount
from beancount.parser import printer

from finance.augur.budget.schema import BucketDef, BucketKind

_CENTS = Decimal("0.01")
_GENERATED = "<augur-budget-exporter>"

# Default Beancount root for each bucket kind when a bucket has no explicit
# ``account``. INFLOW (refunds / insurance payouts) lands in Income as negative
# income; TRANSFER (movement between the user's own accounts) routes through a
# holding account since the other real leg isn't modelled here.
_KIND_ROOT: dict[BucketKind, str] = {
    BucketKind.EXPENSE: "Expenses",
    BucketKind.INCOME: "Income",
    BucketKind.INFLOW: "Income",
    BucketKind.TRANSFER: "Equity:Transfers",
}


@dataclass(frozen=True, slots=True)
class ClassifiedTxn:
    """One live Plaid transaction with its assigned bucket.

    The funding leg is keyed by ``account_id`` (resolved against the funding-account
    map passed to :func:`render_ledger`); the contra leg comes from the bucket. The
    Plaid fields are carried through as ledger metadata for additive reconciliation.
    """

    transaction_id: str
    date: date
    # Plaid sign convention: outflows positive, inflows negative.
    amount: float
    name: str
    account_id: str
    bucket_id: str
    merchant_name: str | None = None
    pfc_primary: str | None = None
    pfc_detailed: str | None = None
    iso_currency_code: str | None = None


def _segment(bucket_id: str) -> str:
    """Turn a snake_case bucket id into a CapWords Beancount account segment.

    Beancount segments forbid underscores, so ``bay_area_psychiatric`` becomes
    ``BayAreaPsychiatric``. Ids are already lowercased ([a-z0-9_]) by the schema.
    """
    return "".join(part.capitalize() for part in bucket_id.split("_"))


def default_account(bucket: BucketDef) -> str:
    """Derive a Beancount account path for a bucket that has no explicit ``account``."""
    return f"{_KIND_ROOT[bucket.kind]}:{_segment(bucket.id)}"


def contra_account(bucket: BucketDef) -> str:
    """The bucket's category leg: its explicit ``account`` or a derived default."""
    return bucket.account or default_account(bucket)


def _cents(value: float) -> Decimal:
    """Quantize to two decimal places, normalizing -0.00 to 0.00."""
    quantized = Decimal(str(value)).quantize(_CENTS)
    return Decimal("0.00") if quantized == 0 else quantized


def _amount(value: float, currency: str) -> Amount:
    return Amount(_cents(value), currency)


def _escape_option(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_ledger(
    txns: Sequence[ClassifiedTxn],
    buckets: Mapping[str, BucketDef],
    funding_accounts: Mapping[str, str],
    *,
    title: str = "Budget",
    operating_currency: str = "USD",
) -> str:
    """Render classified transactions as a deterministic Beancount ledger.

    Args:
        txns: live classified transactions (one bucket each).
        buckets: bucket id -> definition (supplies the contra account path).
        funding_accounts: Plaid account id -> Beancount funding account path.
        title: ledger title (``option "title"``).
        operating_currency: fallback currency and ``option "operating_currency"``.

    Raises:
        KeyError: a txn references a bucket id or account id not in the maps.
    """
    ordered = sorted(txns, key=lambda t: (t.date, t.transaction_id))
    entries: list[data.Directive] = []

    if ordered:
        open_date = ordered[0].date
        used: set[str] = set()
        for txn in ordered:
            used.add(funding_accounts[txn.account_id])
            used.add(contra_account(buckets[txn.bucket_id]))
        entries.extend(
            data.Open(data.new_metadata(_GENERATED, 0), open_date, account, [], None) for account in sorted(used)
        )

    for txn in ordered:
        funding = funding_accounts[txn.account_id]
        contra = contra_account(buckets[txn.bucket_id])
        currency = txn.iso_currency_code or operating_currency

        meta = data.new_metadata(_GENERATED, 0)
        meta["plaid-id"] = txn.transaction_id
        meta["bucket"] = txn.bucket_id
        if txn.pfc_primary:
            meta["plaid-pfc"] = txn.pfc_primary + (f" / {txn.pfc_detailed}" if txn.pfc_detailed else "")

        # Contra (category) leg takes +amount; funding leg takes -amount.
        postings = [
            data.Posting(contra, _amount(txn.amount, currency), None, None, None, None),
            data.Posting(funding, _amount(-txn.amount, currency), None, None, None, None),
        ]
        entries.append(
            data.Transaction(
                meta, txn.date, "*", txn.merchant_name or None, txn.name, data.EMPTY_SET, data.EMPTY_SET, postings
            )
        )

    header = (
        ";; Generated by the augur budget exporter. Do not edit by hand.\n"
        "\n"
        f'option "title" "{_escape_option(title)}"\n'
        f'option "operating_currency" "{_escape_option(operating_currency)}"\n'
        "\n"
    )
    body = "".join(printer.format_entry(entry) for entry in entries)
    return header + body
