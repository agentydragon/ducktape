"""Budget planner configuration: bucket taxonomy and merchant-classification rules.

The augur framework knows nothing about specific user merchants. The deployment's
augur `Config` YAML carries the full rule list (merchants, PFC fallbacks, account
IDs), which augur loads at startup. This file just defines the schemas it populates.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from augur.api.schemas import ApiModel

_ID_PATTERN = r"^[a-z0-9][a-z0-9_]*$"
# Beancount account: a root type followed by one or more capitalized segments.
_BEANCOUNT_ACCOUNT_PATTERN = r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z0-9][A-Za-z0-9-]*)+$"


class TransferDirection(StrEnum):
    """Sign of a transaction. Plaid signs outflows positive, inflows negative."""

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class BucketKind(StrEnum):
    """How a bucket's money flows.

    The earlier model split medical spend into REIMBURSABLE / REIMBURSEMENT and netted
    them on a rolling window. That assumed reimbursements always arrive after charges
    within a fixed window; in practice, insurance disbursements are lumpy and (for some
    providers) actually arrive *before* the charge is submitted. The simpler model:
    every bucket is either money leaving (EXPENSE), money coming in as a refund or
    insurance payout (INFLOW), money moving between the user's own accounts (TRANSFER),
    or earned income (INCOME). The UI groups related buckets with `BucketDef.family`
    and shows both sides without forcing them to balance.
    """

    EXPENSE = "expense"
    INFLOW = "inflow"
    TRANSFER = "transfer"
    INCOME = "income"


class BucketDef(ApiModel):
    """One named spending bucket. Rules route transactions to a `bucket_id`."""

    id: str = Field(pattern=_ID_PATTERN)
    label: str
    kind: BucketKind
    # Optional grouping key. Buckets sharing a family render together as one panel
    # ("medical": esketamine charges + therapy + supplements + insurance premiums +
    # insurance reimbursements). No semantics beyond visual grouping; totals are not
    # auto-netted across family members.
    family: str | None = Field(default=None, pattern=_ID_PATTERN)
    # Sign of every transaction that may land in this bucket. Required on every bucket
    # (no implicit default): rules routing to a bucket inherit its direction as a sign
    # filter, so a descriptor that matches in both directions only fires on the
    # leg whose sign matches the target bucket. For non-transfer kinds, the direction
    # must agree with the kind's semantics (expense -> outflow, inflow/income -> inflow);
    # transfer buckets can pick either.
    direction: TransferDirection
    # Beancount contra ("category") account for this bucket in the exported ledger. The
    # transaction's own Plaid account is the other (funding) leg; this is the category leg.
    # Colon segments form the Fava tree (e.g. "Expenses:Food:Groceries:ThriveMarket").
    # Optional: when omitted, the exporter derives a default path from kind + id.
    account: str | None = Field(default=None, pattern=_BEANCOUNT_ACCOUNT_PATTERN)

    @model_validator(mode="after")
    def _validate_direction(self) -> BucketDef:
        expected = _EXPECTED_DIRECTION.get(self.kind)
        if expected is not None and self.direction != expected:
            raise ValueError(
                f"bucket {self.id!r} kind={self.kind.value} requires direction={expected.value}, "
                f"got {self.direction.value}"
            )
        return self


_EXPECTED_DIRECTION: dict[BucketKind, TransferDirection] = {
    BucketKind.EXPENSE: TransferDirection.OUTFLOW,
    BucketKind.INFLOW: TransferDirection.INFLOW,
    BucketKind.INCOME: TransferDirection.INFLOW,
    # TRANSFER deliberately absent: transfer buckets pick their own direction.
}


class _RuleBase(ApiModel):
    bucket_id: str = Field(pattern=_ID_PATTERN)


class MerchantSubstringRule(_RuleBase):
    """Case-insensitive substring match against `transactions.merchant_name`."""

    kind: Literal["merchant_substring"] = "merchant_substring"
    pattern: str = Field(min_length=1)


class NameSubstringRule(_RuleBase):
    """Case-insensitive substring match against `transactions.name` (the raw descriptor).

    Use this for ACH descriptors where Plaid hasn't promoted a clean merchant name
    (e.g. "ANTHEM BLUE CA5C DES:HCCLAIMPMT" reimbursements, "DD *DOORDASH ..." pass-through)."""

    kind: Literal["name_substring"] = "name_substring"
    pattern: str = Field(min_length=1)


class PfcRule(_RuleBase):
    """Plaid `personal_finance_category` match. `detailed` is optional; if omitted, primary suffices."""

    kind: Literal["pfc"] = "pfc"
    primary: str
    detailed: str | None = None


# --- Condition DSL (Stage A): composable predicates for the `match` rule kind ---
# Leaf conditions mirror the flat rule kinds plus regex / amount / account; composites
# (all_of / any_of / not) nest arbitrarily. A `MatchRule` pairs a condition with a bucket.


class MerchantSubstringCondition(ApiModel):
    """Case-insensitive substring match against `merchant_name` (NULL treated as empty)."""

    kind: Literal["merchant_substring"] = "merchant_substring"
    pattern: str = Field(min_length=1)


class NameSubstringCondition(ApiModel):
    """Case-insensitive substring match against the raw `name` descriptor."""

    kind: Literal["name_substring"] = "name_substring"
    pattern: str = Field(min_length=1)


class MerchantRegexCondition(ApiModel):
    """Case-insensitive POSIX regex against `merchant_name` (NULL treated as empty)."""

    kind: Literal["merchant_regex"] = "merchant_regex"
    pattern: str = Field(min_length=1)


class NameRegexCondition(ApiModel):
    """Case-insensitive POSIX regex against the raw `name` descriptor."""

    kind: Literal["name_regex"] = "name_regex"
    pattern: str = Field(min_length=1)


class PfcCondition(ApiModel):
    """Plaid personal_finance_category match; `detailed` optional (primary alone suffices)."""

    kind: Literal["pfc"] = "pfc"
    primary: str = Field(min_length=1)
    detailed: str | None = None


class AmountCondition(ApiModel):
    """Inclusive numeric bound on the signed Plaid amount (positive = outflow), or its abs value.

    At least one of `min`/`max` is required. Set `use_abs` to bound abs(amount) instead
    (e.g. "any leg >= 5000 regardless of direction")."""

    kind: Literal["amount"] = "amount"
    min: float | None = None
    max: float | None = None
    use_abs: bool = False

    @model_validator(mode="after")
    def _validate_bounds(self) -> AmountCondition:
        if self.min is None and self.max is None:
            raise ValueError("amount condition requires at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"amount condition min ({self.min}) > max ({self.max})")
        return self


class AccountCondition(ApiModel):
    """Match when the transaction's account_id is one of `account_ids`."""

    kind: Literal["account"] = "account"
    account_ids: tuple[str, ...] = Field(min_length=1)


class AllOfCondition(ApiModel):
    """AND: every sub-condition must match."""

    kind: Literal["all_of"] = "all_of"
    conditions: tuple[Condition, ...] = Field(min_length=1)


class AnyOfCondition(ApiModel):
    """OR: at least one sub-condition must match."""

    kind: Literal["any_of"] = "any_of"
    conditions: tuple[Condition, ...] = Field(min_length=1)


class NotCondition(ApiModel):
    """NOT: matches when the inner condition does not."""

    kind: Literal["not"] = "not"
    condition: Condition


Condition = Annotated[
    MerchantSubstringCondition
    | NameSubstringCondition
    | MerchantRegexCondition
    | NameRegexCondition
    | PfcCondition
    | AmountCondition
    | AccountCondition
    | AllOfCondition
    | AnyOfCondition
    | NotCondition,
    Field(discriminator="kind"),
]

for _composite in (AllOfCondition, AnyOfCondition, NotCondition):
    _composite.model_rebuild()


class MatchRule(_RuleBase):
    """Rich rule (Stage A): route to `bucket_id` when `condition` matches. Direction-gated
    by the target bucket like the flat rule kinds; first match wins across all rules."""

    kind: Literal["match"] = "match"
    condition: Condition


Rule = MerchantSubstringRule | NameSubstringRule | PfcRule | MatchRule


class Override(ApiModel):
    """Manual per-transaction classification, keyed on Plaid `transaction_id`.

    Highest priority -- pre-empts every rule -- and NOT direction-gated: an explicit
    human/agent assignment routes the txn to its bucket regardless of sign (e.g. a
    +amount "Returned Payment" reversal -> a transfer bucket). `transaction_id` is stable
    per Plaid Item; a relink mints new ids, so the read model runs a global existence probe
    and reports overrides matching no live row (`stale_overrides`) rather than silently
    no-op'ing. `note` should denormalize a short date/amount/name descriptor so the YAML
    reads without cross-referencing the DB.
    """

    transaction_id: str = Field(min_length=1)
    bucket_id: str = Field(pattern=_ID_PATTERN)
    note: str = Field(min_length=1)


class BudgetSourceConfig(ApiModel):
    """Where to pull transactions from, scoped to a user's accounts."""

    database_url_env: str = "AUGUR_PLAID_DATABASE_URL"
    # Account IDs from `plaid_utils.schema.accounts.account_id`. Empty = all accounts the
    # connection can see (fine for single-user deployments; explicit for shared ones).
    plaid_account_ids: tuple[str, ...] = ()
    iso_currency_code: str = "USD"
    # Earliest date for which the linked accounts provide complete coverage. When set, the
    # snapshot's window start is clamped to this date so historical comparisons aren't
    # skewed by accounts that joined the dataset later (e.g. a Plaid item with a tighter
    # institution-side transaction-history limit than its peers). The wire response carries
    # this date through so the UI can label early months as partial.
    coverage_starts: date | None = None


class BudgetConfig(ApiModel):
    """Top-level budget planner config (optional; absent = budget endpoints return 400)."""

    source: BudgetSourceConfig
    buckets: tuple[BucketDef, ...] = Field(min_length=1)
    # Default bucket per direction for transactions no rule matched. Both required: every bucket
    # is direction-gated, so the fallback must be selectable per transaction sign. The outflow
    # default must point at a `direction=outflow` bucket and likewise for inflow -- enforced by
    # validator below.
    default_outflow_bucket_id: str = Field(pattern=_ID_PATTERN)
    default_inflow_bucket_id: str = Field(pattern=_ID_PATTERN)
    # The full ordered rule list (flat kinds and/or `match` rules). First match wins; a rule
    # only fires on the leg whose sign matches its target bucket's direction.
    rules: tuple[Rule, ...] = ()
    # Per-transaction manual classifications, applied BEFORE any rule and ungated by
    # direction (see `Override`). The primary tool for residual weird cases rules
    # shouldn't generalize (reversals, one-off mislabels).
    overrides: tuple[Override, ...] = ()
    # Transactions with abs(amount) >= this threshold are flagged as "lumpy" (in addition to
    # appearing in their natural bucket). User can re-classify them as one-off vs recurring.
    lumpy_threshold_usd: float = Field(default=500.0, gt=0.0)

    @model_validator(mode="after")
    def _validate_references(self) -> BudgetConfig:
        bucket_by_id = {bucket.id: bucket for bucket in self.buckets}
        for field_name, expected in (
            ("default_outflow_bucket_id", TransferDirection.OUTFLOW),
            ("default_inflow_bucket_id", TransferDirection.INFLOW),
        ):
            bucket_id = getattr(self, field_name)
            target = bucket_by_id.get(bucket_id)
            if target is None:
                raise ValueError(f"{field_name}={bucket_id!r} not in buckets ({sorted(bucket_by_id)})")
            if target.direction != expected:
                raise ValueError(
                    f"{field_name}={bucket_id!r} must reference a direction={expected.value} bucket, "
                    f"got direction={target.direction.value}"
                )
        for rule in self.rules:
            if rule.bucket_id not in bucket_by_id:
                raise ValueError(f"rule references unknown bucket_id {rule.bucket_id!r}")
        seen_override_ids: set[str] = set()
        for override in self.overrides:
            if override.bucket_id not in bucket_by_id:
                raise ValueError(f"override references unknown bucket_id {override.bucket_id!r}")
            if override.transaction_id in seen_override_ids:
                raise ValueError(f"duplicate override for transaction_id {override.transaction_id!r}")
            seen_override_ids.add(override.transaction_id)
        return self
