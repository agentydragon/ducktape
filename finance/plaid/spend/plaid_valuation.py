"""Transport-independent USD valuation of selected Plaid snapshot rows.

Holding rows must come from each account's latest complete capture, never the latest row
of each individual security. Holding-sensitive balances must come from that same capture.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from finance.plaid.spend.portfolio_models import PlaidSourcePolicy, PortfolioPolicy, SourceValuation

_USD_MINOR_UNITS = Decimal(100)


class AccountObservation(BaseModel):
    """Validated balance query row, decoded at the transport boundary."""

    model_config = ConfigDict(allow_inf_nan=False)

    account_id: str
    account_type: str
    account_currency: str | None
    balance_currency: str | None
    current: Decimal | None
    captured_at: datetime | None
    holding_capture_id: int | None = None
    holding_capture_covered: bool | None = None
    holding_capture_has_unapplied_observations: bool = False


class HoldingObservation(BaseModel):
    """Validated position from a complete account capture batch."""

    model_config = ConfigDict(allow_inf_nan=False)

    account_id: str
    security_id: str
    capture_id: int
    institution_value: Decimal | None
    holding_currency: str | None
    captured_at: datetime | None


class PlaidValuationError(ValueError):
    """Selected observations cannot provide a complete, current valuation."""


@dataclass(frozen=True)
class PlaidValuation:
    assets_minor_units: int
    liabilities_minor_units: int
    excluded_minor_units: int
    as_of: datetime
    issues: list[str]

    def source(self) -> SourceValuation:
        return SourceValuation(
            name="plaid",
            status="ready",
            amount_minor_units=self.assets_minor_units,
            as_of=self.as_of,
            issues=self.issues,
        )


def decimal_amount(value: str) -> Decimal | None:
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def usd_minor_units(amount: Decimal) -> int:
    return int((amount * _USD_MINOR_UNITS).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def observation_age_issue(as_of: datetime, now: datetime, max_age_hours: int) -> str | None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        return "A source observation has no timezone."
    if as_of > now:
        return "A source observation is newer than the valuation time."
    if now - as_of > timedelta(hours=max_age_hours):
        return "A source observation is stale."
    return None


def unavailable_source(name: str, issue: str) -> SourceValuation:
    return SourceValuation(name=name, status="unavailable", amount_minor_units=None, as_of=None, issues=[issue])


def corrected_holding_value(policy: PlaidSourcePolicy, *, account_id: str, security_id: str, value: Decimal) -> Decimal:
    """Apply only the policy's exact position correction, leaving other positions unchanged."""
    for correction in policy.holding_value_corrections:
        if correction.account_id == account_id and security_id in correction.security_ids:
            return value * correction.value_multiplier
    return value


def value_plaid_rows(
    policy: PortfolioPolicy,
    *,
    account_rows: Sequence[AccountObservation],
    holding_rows: Sequence[HoldingObservation],
    now: datetime,
) -> PlaidValuation:
    """Value typed observations in USD cents or raise PlaidValuationError.

    Supply holdings only for accounts with configured corrections/exclusions.
    Missing, stale, inconsistent or unreconciled observations never yield totals.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    plaid = policy.sources.plaid
    selected_ids = [*plaid.asset_account_ids, *plaid.liability_account_ids]
    expected_types = dict.fromkeys(plaid.asset_account_ids, "asset")
    expected_types.update(dict.fromkeys(plaid.liability_account_ids, "liability"))
    required_holding_accounts = {exclusion.account_id for exclusion in policy.excluded_securities} | {
        correction.account_id for correction in plaid.holding_value_corrections
    }
    if not selected_ids:
        raise PlaidValuationError("No Plaid accounts are selected.")

    rows_by_id = {row.account_id: row for row in account_rows}
    if len(rows_by_id) != len(account_rows) or set(rows_by_id) != set(selected_ids):
        raise PlaidValuationError("One or more selected Plaid accounts are missing or inactive.")
    if required_holding_accounts.difference(rows_by_id):
        raise PlaidValuationError("A holding selector refers to an account that is not selected.")

    for account_id in required_holding_accounts:
        account_observation = rows_by_id[account_id]
        if account_observation.holding_capture_id is None:
            raise PlaidValuationError("A selected account has no complete holding observation batch.")
        if account_observation.holding_capture_covered is not True:
            raise PlaidValuationError("The latest Item holding capture does not cover a selected account.")
        if account_observation.holding_capture_has_unapplied_observations:
            raise PlaidValuationError("Unapplied holding observations make the latest Item capture unsafe to read.")

    balances: dict[str, Decimal] = {}
    balance_times: dict[str, datetime] = {}
    for account_id, row in rows_by_id.items():
        account_type = row.account_type
        expected = expected_types[account_id]
        valid_types = {"depository", "investment"} if expected == "asset" else {"credit", "loan"}
        if account_type not in valid_types:
            raise PlaidValuationError("A selected Plaid account has an incompatible type.")
        if row.account_currency != "USD" or row.balance_currency != "USD":
            raise PlaidValuationError("A selected Plaid account does not have a confirmed USD balance.")
        amount = row.current
        captured_at = row.captured_at
        if amount is None or not isinstance(captured_at, datetime):
            raise PlaidValuationError("A selected Plaid account has no usable balance observation.")
        issue = observation_age_issue(captured_at, now, plaid.max_age_hours)
        if issue is not None:
            raise PlaidValuationError(issue)
        balances[account_id] = amount
        balance_times[account_id] = captured_at

    excluded_positions = {
        (exclusion.account_id, security_id)
        for exclusion in policy.excluded_securities
        for security_id in exclusion.security_ids
    }
    holding_by_position: dict[tuple[str, str], HoldingObservation] = {}
    holdings_by_account: dict[str, list[HoldingObservation]] = {}
    if {holding.account_id for holding in holding_rows}.difference(required_holding_accounts):
        raise PlaidValuationError("The mirror returned holdings for an account without a holding requirement.")

    for holding in holding_rows:
        position = (holding.account_id, holding.security_id)
        if position in holding_by_position:
            raise PlaidValuationError("A selected holding has an ambiguous observation.")
        holding_by_position[position] = holding
        holdings_by_account.setdefault(position[0], []).append(holding)

    for account_id in required_holding_accounts:
        account_observation = rows_by_id[account_id]
        capture_id = account_observation.holding_capture_id
        assert capture_id is not None  # Required capture coverage was validated above.
        account_holdings = holdings_by_account.get(account_id, [])
        if any(row.capture_id != capture_id for row in account_holdings):
            raise PlaidValuationError("A selected holding does not belong to the account's latest capture.")
        capture_times = {row.captured_at for row in account_holdings} | {account_observation.captured_at}
        if len(capture_times) != 1:
            raise PlaidValuationError("A holding batch has inconsistent observation times.")
        holding_as_of = next(iter(capture_times))
        if not isinstance(holding_as_of, datetime):
            raise PlaidValuationError("A holding has no usable value observation.")
        issue = observation_age_issue(holding_as_of, now, plaid.max_age_hours)
        if issue is not None:
            raise PlaidValuationError(issue)
        balance_times[account_id] = holding_as_of

    excluded_amount = Decimal(0)
    observation_times = list(balance_times.values())
    excluded_by_account: dict[str, Decimal] = {}
    correction_account_ids = {correction.account_id for correction in plaid.holding_value_corrections}
    for account_id in required_holding_accounts:
        account_holdings = holdings_by_account.get(account_id, [])
        adjusted_total = Decimal(0)
        for holding in account_holdings:
            position = (account_id, holding.security_id)
            holding_value = holding.institution_value
            if holding_value is None:
                raise PlaidValuationError("A holding batch has an unusable value.")
            if holding.holding_currency != "USD":
                raise PlaidValuationError("A holding does not have a confirmed USD value.")
            adjusted_value = corrected_holding_value(
                plaid, account_id=account_id, security_id=position[1], value=holding_value
            )
            adjusted_total += adjusted_value
            if position in excluded_positions:
                excluded_amount += adjusted_value
                excluded_by_account[account_id] = excluded_by_account.get(account_id, Decimal(0)) + adjusted_value
        if account_id in correction_account_ids:
            balance = balances[account_id]
            tolerance = max(Decimal(1), abs(balance) * Decimal("0.01"))
            if abs(adjusted_total - balance) > tolerance:
                raise PlaidValuationError("Corrected holdings do not reconcile to the selected account balance.")
        if account_holdings:
            holding_as_of = account_holdings[0].captured_at
            assert isinstance(holding_as_of, datetime)  # Validated above for every required holding account.
            observation_times.append(holding_as_of)

    for account_id, excluded_value in excluded_by_account.items():
        if excluded_value > balances[account_id] + Decimal("0.01"):
            raise PlaidValuationError("Excluded holdings exceed the selected account balance.")

    assets = sum((balances[account_id] for account_id in plaid.asset_account_ids), Decimal(0))
    liabilities = sum((balances[account_id] for account_id in plaid.liability_account_ids), Decimal(0))
    as_of = min(observation_times)
    issues = (
        ["Plaid account and holding observations were captured at meaningfully different times."]
        if max(observation_times) - min(observation_times) > timedelta(minutes=5)
        else []
    )
    return PlaidValuation(
        assets_minor_units=usd_minor_units(assets),
        liabilities_minor_units=usd_minor_units(liabilities),
        excluded_minor_units=usd_minor_units(excluded_amount),
        as_of=as_of,
        issues=issues,
    )
