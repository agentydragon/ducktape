"""Account-inventory projections from the persisted Plaid mirror."""

from __future__ import annotations

import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AccountInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str
    subtype: str | None
    iso_currency_code: str | None
    last_seen_at: datetime.datetime
    detached_at: datetime.datetime | None
    balance_captured_at: datetime.datetime | None
    balance_available: Decimal | None
    balance_current: Decimal | None
    balance_limit: Decimal | None
    balance_iso_currency_code: str | None
    transaction_count: int
    investment_transaction_count: int
    earliest_transaction_date: datetime.date | None
    latest_transaction_date: datetime.date | None


class ItemInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    label: str | None
    institution_name: str | None
    status: str
    accounts_observed_at: datetime.datetime | None
    last_successful_sync_at: datetime.datetime | None
    latest_sync_status: str | None
    latest_sync_started_at: datetime.datetime | None
    latest_sync_finished_at: datetime.datetime | None
    accounts: tuple[AccountInventory, ...]


class AccountDeletionPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    accounts_observed_at: datetime.datetime | None
    latest_sync_run_id: UUID | None
    account: AccountInventory
    transaction_records: int
    investment_transaction_records: int
    balance_snapshot_records: int
    holding_snapshot_records: int
    liability_snapshot_records: int
    earliest_history_date: datetime.date | None
    latest_history_date: datetime.date | None
