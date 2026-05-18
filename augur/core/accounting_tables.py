"""Columnar (polars-backed) storage for the augur accounting trace.

The trace is a parallel ledger emitted alongside the numeric scenario
simulation: every (rollout, month) cell where money moves produces one
journal entry plus one or more postings, with periodic balance snapshots
interleaved. Storing this as `tuple[Posting, ...]` Pydantic objects costs
~3 GB at the gaffer-private default load (15 scenarios × 128 rollouts ×
360 months × ~2-3 postings/cell × ~500 B/Pydantic model). This module
keeps the same data shape but stores it as a small star schema of
`pl.DataFrame`s, with row-by-row materialization to Pydantic on demand.
Joins / filters / aggregations are expressed as polars expressions.

Dim tables (small, deduped):

- `chart_accounts` — one row per distinct ledger account ever posted to.
  Columns mirror the `ChartAccount` Pydantic fields exactly.
- `journal_entry_kinds` — one row per distinct journal-entry batch shape
  (`journal_entry_type`, `cause_type`, `cause_id_prefix`, `actor_id`,
  `policy_id`, `event_id`, `obligation_id_prefix`, `description`). Each
  call to `record_entry(...)` interns one row.

Fact tables (large, one row per active cell):

- `journal_entries` — `(rollout_index, month_index, kind_idx)`.
- `postings` — `(rollout_index, month_index, journal_entry_idx,
  posting_index, chart_account_idx, side, amount_usd, lot_idx,
  liability_idx)`.
- `balance_snapshots` — `(rollout_index, month_index, chart_account_idx,
  balance_usd, quantity)`.

Per-rollout identity:

- `rollout_identity` — one row per rollout, holding the four trajectory
  identity strings (`path_set_id`, `exogenous_path_id`,
  `scenario_input_id`, `projection_trajectory_id`). Filled in by
  `with_trajectory_identity(...)` after the scenario finishes simulating.

Derived strings (`journal_entry_id`, `posting_id`, `obligation_id`,
`chart_account_id`, path-identity fields) are *not* stored on fact
tables; they are materialized on demand from the dim tables, matching
the byte-identical output of `_trace_row_id`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import numpy as np
import polars as pl

from augur.core.accounting import (
    AccountingCause,
    AccountingCauseType,
    AccountingValidationError,
    BalanceSnapshot,
    ChartAccount,
    ChartAccountRole,
    ChartAccountType,
    JournalEntry,
    JournalEntryType,
    Posting,
    PostingSide,
    chart_account_id,
    chart_account_type_for_role,
)

if TYPE_CHECKING:
    from augur.core.policy_runtime import BalanceSnapshotBatch, JournalEntryBatch, PostingBatch


# Polars schemas -------------------------------------------------------------

_CHART_ACCOUNT_SCHEMA = pl.Schema(
    {
        "chart_account_id": pl.String,
        "account_type": pl.String,
        "role": pl.String,
        "actor_id": pl.String,
        "label": pl.String,
        "source_account_id": pl.String,
        "source_asset_id": pl.String,
        "liability_id": pl.String,
        "property_id": pl.String,
        "counterparty_actor_id": pl.String,
    }
)

_JOURNAL_ENTRY_KIND_SCHEMA = pl.Schema(
    {
        "journal_entry_type": pl.String,
        "cause_type": pl.String,
        "cause_id_prefix": pl.String,
        "actor_id": pl.String,
        "policy_id": pl.String,
        "event_id": pl.String,
        "obligation_id_prefix": pl.String,
        "description": pl.String,
    }
)

_JOURNAL_ENTRY_SCHEMA = pl.Schema({"rollout_index": pl.Int32, "month_index": pl.Int32, "kind_idx": pl.Int32})

_POSTING_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int32,
        "month_index": pl.Int32,
        "journal_entry_idx": pl.Int32,
        "posting_index": pl.Int8,
        "chart_account_idx": pl.Int32,
        "side": pl.Int8,
        "amount_usd": pl.Float64,
        "lot_idx": pl.Int32,
        "liability_idx": pl.Int32,
    }
)

_BALANCE_SNAPSHOT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int32,
        "month_index": pl.Int32,
        "chart_account_idx": pl.Int32,
        "balance_usd": pl.Float64,
        "quantity": pl.Float64,
    }
)

_ROLLOUT_IDENTITY_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int32,
        "path_set_id": pl.String,
        "exogenous_path_id": pl.String,
        "scenario_input_id": pl.String,
        "projection_trajectory_id": pl.String,
    }
)

# Side encoding: 0 = DEBIT, 1 = CREDIT. Kept in module-private constants so
# `_SIDE_TO_INT[posting_batch.side]` is a single dict lookup.
_SIDE_TO_INT: dict[PostingSide, int] = {PostingSide.DEBIT: 0, PostingSide.CREDIT: 1}
_INT_TO_SIDE: tuple[PostingSide, ...] = (PostingSide.DEBIT, PostingSide.CREDIT)


def _trace_row_id(prefix: str, *, rollout_index: int, month_index: int) -> str:
    return f"{prefix}:rollout:{rollout_index}:month:{month_index}"


# Column buffers --------------------------------------------------------------


@dataclass
class _ColumnChunkBuffer:
    """Growing column stored as a list of numpy chunks; one concat at finalize."""

    dtype: np.dtype
    _chunks: list[np.ndarray]

    @classmethod
    def empty(cls, dtype: str) -> _ColumnChunkBuffer:
        return cls(dtype=np.dtype(dtype), _chunks=[])

    def extend(self, chunk: np.ndarray) -> None:
        if chunk.size:
            self._chunks.append(chunk.astype(self.dtype, copy=False))

    def fill(self, value: int | float, count: int) -> None:
        if count:
            self._chunks.append(np.full(count, value, dtype=self.dtype))

    def to_array(self) -> np.ndarray:
        if not self._chunks:
            return np.empty(0, dtype=self.dtype)
        return np.concatenate(self._chunks)


@dataclass
class _NullableIntColumnBuffer:
    """Like `_ColumnChunkBuffer` but tracks a null mask alongside int values."""

    dtype: np.dtype
    _value_chunks: list[np.ndarray]
    _valid_chunks: list[np.ndarray]

    @classmethod
    def empty(cls, dtype: str) -> _NullableIntColumnBuffer:
        return cls(dtype=np.dtype(dtype), _value_chunks=[], _valid_chunks=[])

    def extend(self, values: np.ndarray, *, valid: np.ndarray) -> None:
        if values.size:
            self._value_chunks.append(values.astype(self.dtype, copy=False))
            self._valid_chunks.append(valid.astype(np.bool_, copy=False))

    def fill(self, value: int | None, count: int) -> None:
        if not count:
            return
        if value is None:
            self._value_chunks.append(np.zeros(count, dtype=self.dtype))
            self._valid_chunks.append(np.zeros(count, dtype=np.bool_))
        else:
            self._value_chunks.append(np.full(count, value, dtype=self.dtype))
            self._valid_chunks.append(np.ones(count, dtype=np.bool_))

    def to_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        if not self._value_chunks:
            return np.empty(0, dtype=self.dtype), np.empty(0, dtype=np.bool_)
        return np.concatenate(self._value_chunks), np.concatenate(self._valid_chunks)


# Interners -------------------------------------------------------------------


class _ChartAccountInterner:
    """Dedup chart accounts by id; assign each a stable `int32` index."""

    def __init__(self) -> None:
        self._by_id: dict[str, ChartAccount] = {}
        self._idx_by_id: dict[str, int] = {}

    def intern_posting(self, posting: PostingBatch) -> int:
        return self._intern(
            role=posting.role,
            actor_id=posting.actor_id,
            source_account_id=posting.source_account_id,
            source_asset_id=posting.source_asset_id,
            liability_id=posting.liability_id,
            property_id=posting.property_id,
            counterparty_actor_id=posting.counterparty_actor_id,
        )

    def intern_snapshot(self, snapshot: BalanceSnapshotBatch) -> int:
        return self._intern(
            role=snapshot.role,
            actor_id=snapshot.actor_id,
            source_account_id=snapshot.source_account_id,
            source_asset_id=snapshot.source_asset_id,
            liability_id=snapshot.liability_id,
            property_id=snapshot.property_id,
            counterparty_actor_id=snapshot.counterparty_actor_id,
        )

    def _intern(
        self,
        *,
        role: ChartAccountRole,
        actor_id: str | None,
        source_account_id: str | None,
        source_asset_id: str | None,
        liability_id: str | None,
        property_id: str | None,
        counterparty_actor_id: str | None,
    ) -> int:
        account_id = chart_account_id(
            role,
            actor_id=actor_id,
            source_account_id=source_account_id,
            source_asset_id=source_asset_id,
            liability_id=liability_id,
            property_id=property_id,
            counterparty_actor_id=counterparty_actor_id,
        )
        existing_idx = self._idx_by_id.get(account_id)
        if existing_idx is not None:
            return existing_idx
        account = ChartAccount(
            chart_account_id=account_id,
            account_type=chart_account_type_for_role(role),
            role=role,
            actor_id=actor_id,
            source_account_id=source_account_id,
            source_asset_id=source_asset_id,
            liability_id=liability_id,
            property_id=property_id,
            counterparty_actor_id=counterparty_actor_id,
        )
        idx = len(self._by_id)
        self._by_id[account_id] = account
        self._idx_by_id[account_id] = idx
        return idx

    def has(self, account_id: str) -> bool:
        return account_id in self._idx_by_id

    def chart_accounts_by_id(self) -> dict[str, ChartAccount]:
        return dict(self._by_id)

    def build_table(self) -> pl.DataFrame:
        accounts = list(self._by_id.values())
        return pl.DataFrame(
            {
                "chart_account_id": [a.chart_account_id for a in accounts],
                "account_type": [a.account_type.value for a in accounts],
                "role": [a.role.value for a in accounts],
                "actor_id": [a.actor_id for a in accounts],
                "label": [a.label for a in accounts],
                "source_account_id": [a.source_account_id for a in accounts],
                "source_asset_id": [a.source_asset_id for a in accounts],
                "liability_id": [a.liability_id for a in accounts],
                "property_id": [a.property_id for a in accounts],
                "counterparty_actor_id": [a.counterparty_actor_id for a in accounts],
            },
            schema=_CHART_ACCOUNT_SCHEMA,
        )


class _JournalEntryKindInterner:
    """Dedup `JournalEntryBatch` shapes; assign each a stable `int32` index."""

    def __init__(self) -> None:
        self._idx: dict[tuple, int] = {}
        self._kinds: list[_JournalEntryKindRow] = []

    def intern(self, entry: JournalEntryBatch) -> int:
        key = (
            entry.journal_entry_type,
            entry.cause_type,
            entry.cause_id_prefix,
            entry.actor_id,
            entry.policy_id,
            entry.event_id,
            entry.obligation_id_prefix,
            entry.description,
        )
        existing = self._idx.get(key)
        if existing is not None:
            return existing
        idx = len(self._kinds)
        self._idx[key] = idx
        self._kinds.append(
            _JournalEntryKindRow(
                journal_entry_type=entry.journal_entry_type,
                cause_type=entry.cause_type,
                cause_id_prefix=entry.cause_id_prefix,
                actor_id=entry.actor_id,
                policy_id=entry.policy_id,
                event_id=entry.event_id,
                obligation_id_prefix=entry.obligation_id_prefix,
                description=entry.description,
            )
        )
        return idx

    def kinds(self) -> tuple[_JournalEntryKindRow, ...]:
        return tuple(self._kinds)

    def build_table(self) -> pl.DataFrame:
        kinds = self._kinds
        return pl.DataFrame(
            {
                "journal_entry_type": [k.journal_entry_type.value for k in kinds],
                "cause_type": [k.cause_type.value for k in kinds],
                "cause_id_prefix": [k.cause_id_prefix for k in kinds],
                "actor_id": [k.actor_id for k in kinds],
                "policy_id": [k.policy_id for k in kinds],
                "event_id": [k.event_id for k in kinds],
                "obligation_id_prefix": [k.obligation_id_prefix for k in kinds],
                "description": [k.description for k in kinds],
            },
            schema=_JOURNAL_ENTRY_KIND_SCHEMA,
        )


@dataclass(frozen=True)
class _JournalEntryKindRow:
    journal_entry_type: JournalEntryType
    cause_type: AccountingCauseType
    cause_id_prefix: str
    actor_id: str | None
    policy_id: str | None
    event_id: str | None
    obligation_id_prefix: str | None
    description: str | None


class _LiabilityInterner:
    """Map `liability_id` strings to int32 indices."""

    def __init__(self) -> None:
        self._idx: dict[str, int] = {}
        self._ids: list[str] = []

    def intern(self, liability_id: str | None) -> int | None:
        if liability_id is None:
            return None
        existing = self._idx.get(liability_id)
        if existing is not None:
            return existing
        idx = len(self._ids)
        self._idx[liability_id] = idx
        self._ids.append(liability_id)
        return idx

    def liability_ids(self) -> tuple[str, ...]:
        return tuple(self._ids)


# AccountingTrace -------------------------------------------------------------


@dataclass(frozen=True)
class AccountingTrace:
    """Bundle of all accounting tables for a single scenario run.

    `tax_lots`/`liabilities` still live as the existing `tuple[..., ...]`
    Pydantic tuples on `ScenarioRunArrays`; they're kept here as
    references so that `lot_idx` / `liability_idx` columns on the
    `postings` fact table can resolve back to a `TaxLot` / `LiabilityState`
    when materializing a `Posting` Pydantic model.
    """

    chart_accounts: pl.DataFrame
    journal_entry_kinds: pl.DataFrame
    journal_entries: pl.DataFrame
    postings: pl.DataFrame
    balance_snapshots: pl.DataFrame
    rollout_identity: pl.DataFrame
    liability_ids: tuple[str, ...]
    # Source dict for fast `chart_account_id -> ChartAccount` lookup.
    # Built once at finalize and shared across materializations.
    chart_accounts_by_id: dict[str, ChartAccount]

    @classmethod
    def empty(cls) -> AccountingTrace:
        return cls(
            chart_accounts=pl.DataFrame(schema=_CHART_ACCOUNT_SCHEMA),
            journal_entry_kinds=pl.DataFrame(schema=_JOURNAL_ENTRY_KIND_SCHEMA),
            journal_entries=pl.DataFrame(schema=_JOURNAL_ENTRY_SCHEMA),
            postings=pl.DataFrame(schema=_POSTING_SCHEMA),
            balance_snapshots=pl.DataFrame(schema=_BALANCE_SNAPSHOT_SCHEMA),
            rollout_identity=pl.DataFrame(schema=_ROLLOUT_IDENTITY_SCHEMA),
            liability_ids=(),
            chart_accounts_by_id={},
        )

    def with_trajectory_identity(self, by_rollout: dict[int, dict[str, str]]) -> AccountingTrace:
        rollout_indexes = sorted(by_rollout.keys())
        rollout_identity = pl.DataFrame(
            {
                "rollout_index": rollout_indexes,
                "path_set_id": [by_rollout[r].get("path_set_id") for r in rollout_indexes],
                "exogenous_path_id": [by_rollout[r].get("exogenous_path_id") for r in rollout_indexes],
                "scenario_input_id": [by_rollout[r].get("scenario_input_id") for r in rollout_indexes],
                "projection_trajectory_id": [by_rollout[r].get("projection_trajectory_id") for r in rollout_indexes],
            },
            schema=_ROLLOUT_IDENTITY_SCHEMA,
        )
        return replace(self, rollout_identity=rollout_identity)

    def sorted_canonical(self) -> AccountingTrace:
        """Reproduce the byte-stable ordering today's `_sorted_*` produces.

        Today's sort is on the *derived string id*, which packs
        `cause_id_prefix : rollout : month` for journal entries (and the
        same plus `posting_index : side` for postings). We mirror that by
        first remapping `kind_idx` so kinds are ordered by
        `(cause_id_prefix, journal_entry_type, actor_id|"", policy_id|"")`,
        then sorting on the integer keys that capture the same total order.

        Done as a sequence of polars sorts plus index remaps via joins.
        """
        kinds_sorted = (
            self.journal_entry_kinds.with_row_index("old_kind_idx")
            .with_columns(
                _actor_or_empty=pl.col("actor_id").fill_null(""), _policy_or_empty=pl.col("policy_id").fill_null("")
            )
            .sort(["cause_id_prefix", "journal_entry_type", "_actor_or_empty", "_policy_or_empty"])
            .with_row_index("new_kind_idx")
        )
        kind_remap = kinds_sorted.select(["old_kind_idx", "new_kind_idx"])
        new_kinds = kinds_sorted.drop(["old_kind_idx", "new_kind_idx", "_actor_or_empty", "_policy_or_empty"])
        new_journal_entries = (
            self.journal_entries.join(kind_remap, left_on="kind_idx", right_on="old_kind_idx", how="left")
            .with_columns(kind_idx=pl.col("new_kind_idx").cast(pl.Int32))
            .drop("new_kind_idx")
            .select(self.journal_entries.columns)
        )

        je_sorted_with_pos = (
            new_journal_entries.with_row_index("old_je_idx")
            .sort(["month_index", "rollout_index", "kind_idx"])
            .with_row_index("new_je_idx")
        )
        je_remap = je_sorted_with_pos.select(["old_je_idx", "new_je_idx"])
        je_sorted = je_sorted_with_pos.drop(["old_je_idx", "new_je_idx"])

        chart_sorted_with_pos = (
            self.chart_accounts.with_row_index("old_acct_idx").sort("chart_account_id").with_row_index("new_acct_idx")
        )
        chart_remap = chart_sorted_with_pos.select(["old_acct_idx", "new_acct_idx"])
        new_chart_accounts = chart_sorted_with_pos.drop(["old_acct_idx", "new_acct_idx"])

        postings_sorted = (
            self.postings.join(je_remap, left_on="journal_entry_idx", right_on="old_je_idx", how="left")
            .with_columns(journal_entry_idx=pl.col("new_je_idx").cast(pl.Int32))
            .drop("new_je_idx")
            .join(chart_remap, left_on="chart_account_idx", right_on="old_acct_idx", how="left")
            .with_columns(chart_account_idx=pl.col("new_acct_idx").cast(pl.Int32))
            .drop("new_acct_idx")
            .select(self.postings.columns)
            .sort(["month_index", "rollout_index", "journal_entry_idx", "posting_index"])
        )

        snapshots_sorted = (
            self.balance_snapshots.join(chart_remap, left_on="chart_account_idx", right_on="old_acct_idx", how="left")
            .with_columns(chart_account_idx=pl.col("new_acct_idx").cast(pl.Int32))
            .drop("new_acct_idx")
            .select(self.balance_snapshots.columns)
            .sort(["month_index", "rollout_index", "chart_account_idx"])
        )

        new_chart_accounts_by_id = {a.chart_account_id: a for a in _chart_accounts_from_pl(new_chart_accounts)}

        return AccountingTrace(
            chart_accounts=new_chart_accounts,
            journal_entry_kinds=new_kinds,
            journal_entries=je_sorted,
            postings=postings_sorted,
            balance_snapshots=snapshots_sorted,
            rollout_identity=self.rollout_identity,
            liability_ids=self.liability_ids,
            chart_accounts_by_id=new_chart_accounts_by_id,
        )

    # Join graph -----------------------------------------------------------------
    #
    # Storage is polars `DataFrame`s end-to-end (the builder writes numpy
    # chunks directly into typed columns at `finalize`). The fact-table
    # `*_idx` columns are positional references into the corresponding dim
    # tables, so each "joined" view does a positional join via
    # `with_row_index` on the dim side.

    def _postings_joined(self) -> pl.DataFrame:
        """Postings with every column needed to materialize a Pydantic `Posting`.

        Joins postings → journal_entries (for the entry's rollout/month, used
        in id derivation) → journal_entry_kinds (for `cause_id_prefix`) →
        chart_accounts (for `chart_account_id`) → rollout_identity (for the
        four trajectory-identity strings).
        """
        return (
            self.postings.join(
                self.journal_entries.with_row_index("je_pos").rename(
                    {"rollout_index": "je_rollout", "month_index": "je_month"}
                ),
                left_on="journal_entry_idx",
                right_on="je_pos",
                how="inner",
            )
            .join(
                self.journal_entry_kinds.with_row_index("kind_pos"),
                left_on="kind_idx",
                right_on="kind_pos",
                how="inner",
            )
            .join(
                self.chart_accounts.with_row_index("acct_pos"),
                left_on="chart_account_idx",
                right_on="acct_pos",
                how="inner",
            )
            .join(self.rollout_identity, on="rollout_index", how="left")
        )

    def _journal_entries_joined(self) -> pl.DataFrame:
        """JournalEntries with kind + rollout identity columns attached."""
        return self.journal_entries.join(
            self.journal_entry_kinds.with_row_index("kind_pos"), left_on="kind_idx", right_on="kind_pos", how="inner"
        ).join(self.rollout_identity, on="rollout_index", how="left")

    def _balance_snapshots_joined(self) -> pl.DataFrame:
        return self.balance_snapshots.join(
            self.chart_accounts.with_row_index("acct_pos"),
            left_on="chart_account_idx",
            right_on="acct_pos",
            how="inner",
        ).join(self.rollout_identity, on="rollout_index", how="left")

    # Materialization to Pydantic ------------------------------------------------

    def chart_accounts_tuple(self) -> tuple[ChartAccount, ...]:
        return _chart_accounts_from_pl(self.chart_accounts)

    def journal_entries_tuple(self) -> tuple[JournalEntry, ...]:
        return _journal_entries_from_pl(self._journal_entries_joined())

    def postings_tuple(self) -> tuple[Posting, ...]:
        return _postings_from_pl(self._postings_joined(), liability_ids=self.liability_ids)

    def balance_snapshots_tuple(self) -> tuple[BalanceSnapshot, ...]:
        return _balance_snapshots_from_pl(self._balance_snapshots_joined())

    # Filters --------------------------------------------------------------------

    def filter_postings(
        self, *, rollout: int | None = None, side: PostingSide | None = None, role: ChartAccountRole | None = None
    ) -> tuple[Posting, ...]:
        df = self._postings_joined()
        if rollout is not None:
            df = df.filter(pl.col("rollout_index") == rollout)
        if side is not None:
            df = df.filter(pl.col("side") == _SIDE_TO_INT[side])
        if role is not None:
            df = df.filter(pl.col("role") == role.value)
        return _postings_from_pl(df, liability_ids=self.liability_ids)

    def filter_journal_entries(
        self, *, rollout: int | None = None, journal_entry_type: JournalEntryType | None = None
    ) -> tuple[JournalEntry, ...]:
        df = self._journal_entries_joined()
        if journal_entry_type is not None:
            df = df.filter(pl.col("journal_entry_type") == journal_entry_type.value)
        if rollout is not None:
            df = df.filter(pl.col("rollout_index") == rollout)
        return _journal_entries_from_pl(df)

    def filter_balance_snapshots(
        self, *, rollout: int | None = None, role: ChartAccountRole | None = None
    ) -> tuple[BalanceSnapshot, ...]:
        df = self._balance_snapshots_joined()
        if role is not None:
            df = df.filter(pl.col("role") == role.value)
        if rollout is not None:
            df = df.filter(pl.col("rollout_index") == rollout)
        return _balance_snapshots_from_pl(df)

    def filter_chart_accounts(self, *, role: ChartAccountRole | None = None) -> tuple[ChartAccount, ...]:
        df = self.chart_accounts
        if role is not None:
            df = df.filter(pl.col("role") == role.value)
        return _chart_accounts_from_pl(df)

    # Aggregation kernels ------------------------------------------------------

    def posting_amount_matrix(
        self,
        *,
        rollout_count: int,
        month_index: np.ndarray,
        role: ChartAccountRole,
        side: PostingSide | None = None,
        journal_entry_type: JournalEntryType | None = None,
    ) -> np.ndarray:
        """Sum posting amounts matching the filter into a `(rollout_count,
        len(month_index))` matrix indexed by month-position.

        Filter + join + scatter, expressed in polars. The polars filter pushes
        through the join graph as a single relational query; what comes out
        is just three numpy columns to drive `np.add.at`.
        """
        df = self._postings_joined().filter(pl.col("role") == role.value)
        if side is not None:
            df = df.filter(pl.col("side") == _SIDE_TO_INT[side])
        if journal_entry_type is not None:
            df = df.filter(pl.col("journal_entry_type") == journal_entry_type.value)
        return _aggregate_to_matrix(
            df, rollout_count=rollout_count, month_index=month_index, amount_column="amount_usd", fact_label="posting"
        )

    def balance_snapshot_amount_matrix(
        self, *, rollout_count: int, month_index: np.ndarray, role: ChartAccountRole
    ) -> np.ndarray:
        df = self._balance_snapshots_joined().filter(pl.col("role") == role.value)
        return _aggregate_to_matrix(
            df,
            rollout_count=rollout_count,
            month_index=month_index,
            amount_column="balance_usd",
            fact_label="balance snapshot",
        )

    def journal_entry_row(self, idx: int) -> JournalEntry:
        if idx < 0 or idx >= self.journal_entries.height:
            raise IndexError(idx)
        joined = self._journal_entries_joined().slice(idx, 1)
        return _journal_entries_from_pl(joined)[0]


def _build_journal_entry(
    *,
    rollout_index: int,
    month_index: int,
    cause_id_prefix: str,
    journal_entry_type: JournalEntryType,
    cause_type: AccountingCauseType,
    actor_id: str | None,
    policy_id: str | None,
    event_id: str | None,
    obligation_id_prefix: str | None,
    description: str | None,
    identity: dict[str, str | None],
) -> JournalEntry:
    journal_entry_id = _trace_row_id(cause_id_prefix, rollout_index=rollout_index, month_index=month_index)
    obligation_id = (
        _trace_row_id(obligation_id_prefix, rollout_index=rollout_index, month_index=month_index)
        if obligation_id_prefix is not None
        else None
    )
    return JournalEntry(
        journal_entry_id=journal_entry_id,
        rollout_index=rollout_index,
        month_index=month_index,
        journal_entry_type=journal_entry_type,
        actor_id=actor_id,
        policy_id=policy_id,
        event_id=event_id,
        obligation_id=obligation_id,
        description=description,
        cause=AccountingCause(
            cause_type=cause_type,
            cause_id=journal_entry_id,
            policy_id=policy_id,
            event_id=event_id,
            obligation_id=obligation_id,
        ),
        path_set_id=identity.get("path_set_id"),
        exogenous_path_id=identity.get("exogenous_path_id"),
        scenario_input_id=identity.get("scenario_input_id"),
        projection_trajectory_id=identity.get("projection_trajectory_id"),
    )


def _chart_accounts_from_pl(df: pl.DataFrame) -> tuple[ChartAccount, ...]:
    if df.height == 0:
        return ()
    return tuple(
        ChartAccount(
            chart_account_id=row["chart_account_id"],
            account_type=ChartAccountType(row["account_type"]),
            role=ChartAccountRole(row["role"]),
            actor_id=row["actor_id"],
            label=row["label"],
            source_account_id=row["source_account_id"],
            source_asset_id=row["source_asset_id"],
            liability_id=row["liability_id"],
            property_id=row["property_id"],
            counterparty_actor_id=row["counterparty_actor_id"],
        )
        for row in df.iter_rows(named=True)
    )


def _journal_entries_from_pl(df: pl.DataFrame) -> tuple[JournalEntry, ...]:
    """Materialize `JournalEntry` Pydantic models from a `_journal_entries_joined`-shaped frame."""
    if df.height == 0:
        return ()
    return tuple(
        _build_journal_entry(
            rollout_index=row["rollout_index"],
            month_index=row["month_index"],
            cause_id_prefix=row["cause_id_prefix"],
            journal_entry_type=JournalEntryType(row["journal_entry_type"]),
            cause_type=AccountingCauseType(row["cause_type"]),
            actor_id=row["actor_id"],
            policy_id=row["policy_id"],
            event_id=row["event_id"],
            obligation_id_prefix=row["obligation_id_prefix"],
            description=row["description"],
            identity={
                "path_set_id": row["path_set_id"],
                "exogenous_path_id": row["exogenous_path_id"],
                "scenario_input_id": row["scenario_input_id"],
                "projection_trajectory_id": row["projection_trajectory_id"],
            },
        )
        for row in df.iter_rows(named=True)
    )


def _postings_from_pl(df: pl.DataFrame, *, liability_ids: tuple[str, ...]) -> tuple[Posting, ...]:
    """Materialize `Posting` Pydantic models from a `_postings_joined`-shaped frame."""
    if df.height == 0:
        return ()
    out: list[Posting] = []
    for row in df.iter_rows(named=True):
        side = _INT_TO_SIDE[row["side"]]
        journal_entry_id = _trace_row_id(
            row["cause_id_prefix"], rollout_index=row["je_rollout"], month_index=row["je_month"]
        )
        posting_id = f"{journal_entry_id}:posting:{row['posting_index']}:{side.value}"
        liab_idx = row["liability_idx"]
        out.append(
            Posting(
                posting_id=posting_id,
                journal_entry_id=journal_entry_id,
                rollout_index=row["rollout_index"],
                month_index=row["month_index"],
                chart_account_id=row["chart_account_id"],
                side=side,
                amount_usd=row["amount_usd"],
                liability_id=liability_ids[liab_idx] if liab_idx is not None else None,
                path_set_id=row["path_set_id"],
                exogenous_path_id=row["exogenous_path_id"],
                scenario_input_id=row["scenario_input_id"],
                projection_trajectory_id=row["projection_trajectory_id"],
            )
        )
    return tuple(out)


def _balance_snapshots_from_pl(df: pl.DataFrame) -> tuple[BalanceSnapshot, ...]:
    if df.height == 0:
        return ()
    return tuple(
        BalanceSnapshot(
            rollout_index=row["rollout_index"],
            month_index=row["month_index"],
            chart_account_id=row["chart_account_id"],
            balance_usd=row["balance_usd"],
            quantity=row["quantity"],
            path_set_id=row["path_set_id"],
            exogenous_path_id=row["exogenous_path_id"],
            scenario_input_id=row["scenario_input_id"],
            projection_trajectory_id=row["projection_trajectory_id"],
        )
        for row in df.iter_rows(named=True)
    )


def _aggregate_to_matrix(
    df: pl.DataFrame, *, rollout_count: int, month_index: np.ndarray, amount_column: str, fact_label: str
) -> np.ndarray:
    """Group `df` by `(rollout_index, month_index)`, sum `amount_column`, and
    scatter the result into a dense `(rollout_count, len(month_index))` matrix
    indexed by month-position. Rows referencing months outside the horizon
    raise.

    Uses a polars `join` with the horizon's month → position mapping to do the
    lookup (rather than reaching back into numpy for it), and a polars
    `group_by` + `agg(sum)` to pre-aggregate before the dense scatter.
    """
    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    if df.height == 0:
        return matrix

    horizon = pl.DataFrame(
        {"month_index": list(month_index.tolist()), "month_position": list(range(len(month_index)))},
        schema={"month_index": pl.Int32, "month_position": pl.Int32},
    )
    matched = df.join(horizon, on="month_index", how="inner")
    if matched.height != df.height:
        offenders = df.join(horizon, on="month_index", how="anti")
        first = int(offenders["month_index"].head(1)[0])
        raise ValueError(f"{fact_label} has month outside result horizon: {first}")

    grouped = matched.group_by(["rollout_index", "month_position"]).agg(pl.col(amount_column).sum())
    rollouts = grouped["rollout_index"].to_numpy()
    positions = grouped["month_position"].to_numpy()
    amounts = grouped[amount_column].to_numpy()
    matrix[rollouts, positions] = amounts
    return matrix


def _nullable_int_series(name: str, values: np.ndarray, valid: np.ndarray) -> pl.Series:
    """Build a nullable int polars Series from a (values, valid_mask) pair."""
    return _nullable_series(name, values, valid, dtype=pl.Int32)


def _nullable_float_series(name: str, values: np.ndarray, valid: np.ndarray) -> pl.Series:
    return _nullable_series(name, values, valid, dtype=pl.Float64)


def _nullable_series(name: str, values: np.ndarray, valid: np.ndarray, *, dtype: type[pl.DataType]) -> pl.Series:
    raw = pl.Series(name, values, dtype=dtype)
    mask = pl.Series("_valid", valid, dtype=pl.Boolean)
    return pl.select(pl.when(mask).then(raw).otherwise(None).alias(name)).to_series()


# Builder ---------------------------------------------------------------------


class AccountingTraceBuilder:
    """Vectorized builder for the columnar accounting trace.

    Maintains numpy-chunk buffers per fact-table column, interners for
    chart accounts and journal-entry kinds, and a small string interner
    for `liability_id`. `record_entry` and `record_snapshot` append in
    bulk per (rollout, month) batch; `finalize` concatenates the chunks
    and wraps them in `pl.DataFrame`s for the returned `AccountingTrace`.
    """

    def __init__(self) -> None:
        self._chart_accounts = _ChartAccountInterner()
        self._journal_kinds = _JournalEntryKindInterner()
        self._liabilities = _LiabilityInterner()

        # JournalEntryTable buffers.
        self._je_rollout = _ColumnChunkBuffer.empty("int32")
        self._je_month = _ColumnChunkBuffer.empty("int32")
        self._je_kind = _ColumnChunkBuffer.empty("int32")
        self._je_count = 0

        # PostingTable buffers.
        self._po_rollout = _ColumnChunkBuffer.empty("int32")
        self._po_month = _ColumnChunkBuffer.empty("int32")
        self._po_je_idx = _ColumnChunkBuffer.empty("int32")
        self._po_index = _ColumnChunkBuffer.empty("int8")
        self._po_acct = _ColumnChunkBuffer.empty("int32")
        self._po_side = _ColumnChunkBuffer.empty("int8")
        self._po_amount = _ColumnChunkBuffer.empty("float64")
        self._po_lot = _NullableIntColumnBuffer.empty("int32")
        self._po_liab = _NullableIntColumnBuffer.empty("int32")

        # BalanceSnapshotTable buffers.
        self._bs_rollout = _ColumnChunkBuffer.empty("int32")
        self._bs_month = _ColumnChunkBuffer.empty("int32")
        self._bs_acct = _ColumnChunkBuffer.empty("int32")
        self._bs_balance = _ColumnChunkBuffer.empty("float64")
        self._bs_quantity = _NullableIntColumnBuffer.empty("float64")  # treated as nullable float

    # Public accessors used by engine-internal aggregations during the run.
    @property
    def chart_accounts_by_id(self) -> dict[str, ChartAccount]:
        return self._chart_accounts.chart_accounts_by_id()

    def record_entry(
        self, *, month_index: int | np.ndarray, entry: JournalEntryBatch, amount_multiplier: np.ndarray | None = None
    ) -> None:
        month_values, posting_amounts = _normalized_posting_amounts(
            month_index=month_index, postings=entry.postings, amount_multiplier=amount_multiplier
        )
        if not posting_amounts:
            return

        active = np.zeros(posting_amounts[0][1].shape, dtype=np.bool_)
        for _, amount_matrix in posting_amounts:
            np.logical_or(active, amount_matrix > 0, out=active)
        rollouts, months = np.nonzero(active)
        n = int(rollouts.size)
        if n == 0:
            return

        kind_idx = self._journal_kinds.intern(entry)
        je_start = self._je_count
        self._je_rollout.extend(rollouts)
        self._je_month.extend(month_values[months])
        self._je_kind.fill(kind_idx, n)
        self._je_count += n

        # Grid that maps (rollout, month_position) -> je_idx within this batch.
        je_idx_grid = np.full(active.shape, -1, dtype=np.int32)
        je_idx_grid[rollouts, months] = np.arange(je_start, je_start + n, dtype=np.int32)

        for posting_index, (posting_batch, amount_matrix) in enumerate(posting_amounts):
            p_active = amount_matrix > 0
            p_r, p_m = np.nonzero(p_active)
            p_n = int(p_r.size)
            if p_n == 0:
                continue
            acct_idx = self._chart_accounts.intern_posting(posting_batch)
            self._po_rollout.extend(p_r)
            self._po_month.extend(month_values[p_m])
            self._po_je_idx.extend(je_idx_grid[p_r, p_m])
            self._po_index.fill(posting_index, p_n)
            self._po_acct.fill(acct_idx, p_n)
            self._po_side.fill(_SIDE_TO_INT[posting_batch.side], p_n)
            self._po_amount.extend(amount_matrix[p_r, p_m])
            liab_idx = self._liabilities.intern(posting_batch.liability_id)
            self._po_liab.fill(liab_idx, p_n)
            self._po_lot.fill(None, p_n)

    def record_snapshot(self, *, month_index: np.ndarray, snapshot: BalanceSnapshotBatch) -> None:
        amount = np.asarray(snapshot.amount_usd, dtype="float64")
        if amount.ndim != 2:
            raise ValueError("balance snapshot amount_usd must be rollout/month shaped")
        rollouts, months = np.nonzero(amount != 0)
        n = int(rollouts.size)
        if n == 0:
            # Still register the chart account so accumulators can see it.
            self._chart_accounts.intern_snapshot(snapshot)
            return
        acct_idx = self._chart_accounts.intern_snapshot(snapshot)
        self._bs_rollout.extend(rollouts)
        self._bs_month.extend(month_index[months])
        self._bs_acct.fill(acct_idx, n)
        self._bs_balance.extend(amount[rollouts, months])
        self._bs_quantity.fill(None, n)

    def finalize(self) -> AccountingTrace:
        po_lot = _nullable_int_series("lot_idx", *self._po_lot.to_arrays())
        po_liab = _nullable_int_series("liability_idx", *self._po_liab.to_arrays())
        bs_quantity = _nullable_float_series("quantity", *self._bs_quantity.to_arrays())

        journal_entries_df = pl.DataFrame(
            {
                "rollout_index": self._je_rollout.to_array(),
                "month_index": self._je_month.to_array(),
                "kind_idx": self._je_kind.to_array(),
            },
            schema=_JOURNAL_ENTRY_SCHEMA,
        )

        postings_df = pl.DataFrame(
            {
                "rollout_index": self._po_rollout.to_array(),
                "month_index": self._po_month.to_array(),
                "journal_entry_idx": self._po_je_idx.to_array(),
                "posting_index": self._po_index.to_array(),
                "chart_account_idx": self._po_acct.to_array(),
                "side": self._po_side.to_array(),
                "amount_usd": self._po_amount.to_array(),
                "lot_idx": po_lot,
                "liability_idx": po_liab,
            },
            schema=_POSTING_SCHEMA,
        )

        balance_snapshots_df = pl.DataFrame(
            {
                "rollout_index": self._bs_rollout.to_array(),
                "month_index": self._bs_month.to_array(),
                "chart_account_idx": self._bs_acct.to_array(),
                "balance_usd": self._bs_balance.to_array(),
                "quantity": bs_quantity,
            },
            schema=_BALANCE_SNAPSHOT_SCHEMA,
        )

        return AccountingTrace(
            chart_accounts=self._chart_accounts.build_table(),
            journal_entry_kinds=self._journal_kinds.build_table(),
            journal_entries=journal_entries_df,
            postings=postings_df,
            balance_snapshots=balance_snapshots_df,
            rollout_identity=pl.DataFrame(schema=_ROLLOUT_IDENTITY_SCHEMA),
            liability_ids=self._liabilities.liability_ids(),
            chart_accounts_by_id=self._chart_accounts.chart_accounts_by_id(),
        )


# Helpers shared between builder and other engine modules ---------------------


def _normalized_posting_amounts(
    *, month_index: int | np.ndarray, postings: tuple[PostingBatch, ...], amount_multiplier: np.ndarray | None = None
) -> tuple[np.ndarray, list[tuple[PostingBatch, np.ndarray]]]:
    """Bring all per-posting amount arrays to a shared `(rollouts, months)` shape.

    Mirrors the existing free function in `augur/core/scenario_engine.py`. Kept
    here so the builder doesn't pull in scenario_engine; that file imports
    this helper instead of defining it.
    """
    month_values = np.asarray([month_index], dtype="int64") if isinstance(month_index, int) else month_index
    normalized: list[tuple[PostingBatch, np.ndarray]] = []
    for posting in postings:
        amount_usd = np.asarray(posting.amount_usd, dtype="float64")
        if amount_usd.ndim == 1:
            amount_usd = amount_usd[:, None]
        if amount_usd.ndim != 2:
            raise ValueError("posting amount_usd must be rollout or rollout/month shaped")
        if amount_multiplier is not None:
            multiplier = np.asarray(amount_multiplier, dtype="float64")
            if multiplier.ndim == 1:
                multiplier = multiplier[:, None]
            amount_usd = amount_usd * multiplier
        if amount_usd.shape[1] != len(month_values):
            raise ValueError(
                f"posting month dimension {amount_usd.shape[1]} does not match month_index length {len(month_values)}"
            )
        normalized.append((posting, amount_usd))
    return month_values, normalized


def validate_trace(trace: AccountingTrace, *, tolerance_usd: float = 0.005) -> None:
    """Columnar equivalent of `validate_accounting_trace`. Checks the
    invariants that the columnar representation does not auto-satisfy:

    - Every `JournalEntry` row has at least one posting.
    - Per-`JournalEntry` debit/credit balance within `tolerance_usd`.

    Reference resolution (every posting's `journal_entry_idx` /
    `chart_account_idx` is in range, no duplicate ids) is structural:
    the indices are `pl.Int32` foreign keys built by interners, so they
    can never reference unknown rows by construction.
    """
    # Every journal entry must have at least one posting referring to it.
    posting_je_idxs = set(trace.postings["journal_entry_idx"].unique().to_list())
    missing = (
        trace.journal_entries.with_row_index("je_idx").filter(~pl.col("je_idx").is_in(posting_je_idxs)).sort("je_idx")
    )
    if missing.height > 0:
        first_idx = int(missing.row(0, named=True)["je_idx"])
        raise AccountingValidationError(
            f"{missing.height} journal entry/entries have no postings; first idx: {first_idx}"
        )

    # Per-journal-entry debit/credit balance.
    unbalanced = (
        trace.postings.with_columns(
            signed=pl.when(pl.col("side") == 0).then(pl.col("amount_usd")).otherwise(-pl.col("amount_usd"))
        )
        .group_by("journal_entry_idx")
        .agg(net=pl.col("signed").sum())
        .filter(pl.col("net").abs() > tolerance_usd)
        .sort("journal_entry_idx")
    )
    if unbalanced.height > 0:
        first = unbalanced.row(0, named=True)
        raise AccountingValidationError(
            f"{unbalanced.height} journal entry/entries unbalanced; "
            f"first idx={int(first['journal_entry_idx'])} net debits-credits={float(first['net']):.4f}"
        )


__all__ = ["AccountingTrace", "AccountingTraceBuilder", "validate_trace"]
