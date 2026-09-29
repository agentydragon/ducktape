"""Pydantic views of shaped Plaid payloads consumed by sync and storage boundaries.

REST shapes follow Plaid's official OpenAPI document:
https://github.com/plaid/plaid-openapi/blob/master/2020-09-14.yml. The models type the fields this
service reads while allowing additional fields so Plaid can extend responses without breaking us.
The SDK client itself uses plaid-python's generated request and response classes directly.
Plaid's transaction webhook envelope and verification claims follow the webhook documentation:
https://plaid.com/docs/transactions/webhooks/ and https://plaid.com/docs/api/webhooks/webhook-verification/.
"""

from __future__ import annotations

import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class PlaidPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class PlaidApiResponse(PlaidPayload):
    request_id: str | None = Field(default=None, validation_alias=AliasChoices("request_id", "requestId"))


class PlaidBalance(PlaidPayload):
    available: float | None = None
    current: float | None = None
    limit: float | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None


class PlaidAccount(PlaidPayload):
    account_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    type: str
    subtype: str | None = None
    balances: PlaidBalance | None = None


class AccountsGetResponse(PlaidApiResponse):
    accounts: list[PlaidAccount] | None = None


class PlaidItem(PlaidPayload):
    item_id: str | None = None
    institution_id: str | None = None
    institution_name: str | None = None
    products: list[str] | None = None
    billed_products: list[str] | None = None
    webhook: str | None = None


class ItemGetResponse(PlaidApiResponse):
    item: PlaidItem = Field(default_factory=PlaidItem)


class ItemWebhookUpdateResponse(PlaidApiResponse):
    pass


class PlaidPersonalFinanceCategory(PlaidPayload):
    primary: str | None = None
    detailed: str | None = None
    confidence_level: str | None = None


class PlaidTransaction(PlaidPayload):
    transaction_id: str
    account_id: str
    date: datetime.date
    amount: float
    name: str
    pending: bool
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None
    merchant_name: str | None = None
    pending_transaction_id: str | None = None
    personal_finance_category: PlaidPersonalFinanceCategory | None = None


class TransactionsGetResponse(PlaidApiResponse):
    total_transactions: int
    transactions: list[PlaidTransaction] | None = None


class PlaidRemovedTransaction(PlaidPayload):
    transaction_id: str


class TransactionsSyncResponse(PlaidApiResponse):
    accounts: list[PlaidAccount]
    added: list[PlaidTransaction]
    modified: list[PlaidTransaction]
    removed: list[PlaidRemovedTransaction]
    next_cursor: str
    has_more: bool
    transactions_update_status: str


class PlaidSecurity(PlaidPayload):
    security_id: str
    name: str | None = None
    ticker_symbol: str | None = None
    type: str | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None


class PlaidHolding(PlaidPayload):
    account_id: str
    security_id: str
    quantity: float | None = None
    cost_basis: float | None = None
    institution_price: float | None = None
    institution_value: float | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None


class InvestmentsHoldingsGetResponse(PlaidApiResponse):
    securities: list[PlaidSecurity] | None = None
    holdings: list[PlaidHolding] | None = None
    accounts: list[PlaidAccount] | None = None


class PlaidInvestmentTransaction(PlaidPayload):
    investment_transaction_id: str
    account_id: str
    date: datetime.date
    security_id: str | None = None
    amount: float | None = None
    quantity: float | None = None
    price: float | None = None
    fees: float | None = None
    type: str | None = None
    subtype: str | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None


class InvestmentsTransactionsGetResponse(PlaidApiResponse):
    total_investment_transactions: int
    investment_transactions: list[PlaidInvestmentTransaction] | None = None
    accounts: list[PlaidAccount] | None = None


class PlaidLiabilityEntry(PlaidPayload):
    account_id: str


class PlaidLiabilities(PlaidPayload):
    credit: list[PlaidLiabilityEntry] | None = None
    mortgage: list[PlaidLiabilityEntry] | None = None
    student: list[PlaidLiabilityEntry] | None = None


class LiabilitiesGetResponse(PlaidApiResponse):
    liabilities: PlaidLiabilities | None = None
    accounts: list[PlaidAccount] | None = None


class PlaidWebhookEnvelope(PlaidPayload):
    webhook_type: str
    webhook_code: str
    item_id: str | None = None


class PlaidWebhookVerificationHeader(PlaidPayload):
    alg: str
    kid: str


class PlaidWebhookVerificationClaims(PlaidPayload):
    iat: int = Field(strict=True)
    request_body_sha256: str = Field(strict=True)


class PlaidError(PlaidPayload):
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    display_message: str | None = None
    documentation_url: str | None = None
    request_id: str | None = Field(default=None, validation_alias=AliasChoices("request_id", "requestId"))
