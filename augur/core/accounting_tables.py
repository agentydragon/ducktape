"""Columnar (PyArrow-backed) storage for the augur accounting trace.

The trace is a parallel ledger emitted alongside the numeric scenario
simulation: every (rollout, month) cell where money moves produces one
journal entry plus one or more postings, with periodic balance snapshots
interleaved. Storing this as `tuple[Posting, ...]` Pydantic objects costs
~3 GB at the gaffer-private default load (15 scenarios × 128 rollouts ×
360 months × ~2-3 postings/cell × ~500 B/Pydantic model). This module
keeps the same data shape but stores it as a small star schema of PyArrow
tables, with row-by-row materialization to Pydantic on demand.

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
from typing import TYPE_CHECKING, cast

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc

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

# `pyarrow.compute` kernel functions are registered dynamically at C-extension
# load time, so the bundled stubs don't list them as attributes. Bind once at
# module top so we type-ignore the lookup in one place instead of every call.
# Only `sort_indices` survives the polars migration — filters and `is_in`
# moved to polars expressions, but `sorted_canonical` still uses pyarrow's
# sort+take + index remapping path.
_pc_sort_indices = pc.sort_indices  # type: ignore[attr-defined]


# Arrow schemas ---------------------------------------------------------------

_CHART_ACCOUNT_SCHEMA = pa.schema(
    [
        pa.field("chart_account_id", pa.string(), nullable=False),
        pa.field("account_type", pa.string(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("actor_id", pa.string(), nullable=True),
        pa.field("label", pa.string(), nullable=True),
        pa.field("source_account_id", pa.string(), nullable=True),
        pa.field("source_asset_id", pa.string(), nullable=True),
        pa.field("liability_id", pa.string(), nullable=True),
        pa.field("property_id", pa.string(), nullable=True),
        pa.field("counterparty_actor_id", pa.string(), nullable=True),
    ]
)

_JOURNAL_ENTRY_KIND_SCHEMA = pa.schema(
    [
        pa.field("journal_entry_type", pa.string(), nullable=False),
        pa.field("cause_type", pa.string(), nullable=False),
        pa.field("cause_id_prefix", pa.string(), nullable=False),
        pa.field("actor_id", pa.string(), nullable=True),
        pa.field("policy_id", pa.string(), nullable=True),
        pa.field("event_id", pa.string(), nullable=True),
        pa.field("obligation_id_prefix", pa.string(), nullable=True),
        pa.field("description", pa.string(), nullable=True),
    ]
)

_JOURNAL_ENTRY_SCHEMA = pa.schema(
    [
        pa.field("rollout_index", pa.int32(), nullable=False),
        pa.field("month_index", pa.int32(), nullable=False),
        pa.field("kind_idx", pa.int32(), nullable=False),
    ]
)

_POSTING_SCHEMA = pa.schema(
    [
        pa.field("rollout_index", pa.int32(), nullable=False),
        pa.field("month_index", pa.int32(), nullable=False),
        pa.field("journal_entry_idx", pa.int32(), nullable=False),
        pa.field("posting_index", pa.int8(), nullable=False),
        pa.field("chart_account_idx", pa.int32(), nullable=False),
        pa.field("side", pa.int8(), nullable=False),
        pa.field("amount_usd", pa.float64(), nullable=False),
        pa.field("lot_idx", pa.int32(), nullable=True),
        pa.field("liability_idx", pa.int32(), nullable=True),
    ]
)

_BALANCE_SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("rollout_index", pa.int32(), nullable=False),
        pa.field("month_index", pa.int32(), nullable=False),
        pa.field("chart_account_idx", pa.int32(), nullable=False),
        pa.field("balance_usd", pa.float64(), nullable=False),
        pa.field("quantity", pa.float64(), nullable=True),
    ]
)

_ROLLOUT_IDENTITY_SCHEMA = pa.schema(
    [
        pa.field("rollout_index", pa.int32(), nullable=False),
        pa.field("path_set_id", pa.string(), nullable=True),
        pa.field("exogenous_path_id", pa.string(), nullable=True),
        pa.field("scenario_input_id", pa.string(), nullable=True),
        pa.field("projection_trajectory_id", pa.string(), nullable=True),
    ]
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

    def build_table(self) -> pa.Table:
        accounts = list(self._by_id.values())
        return pa.table(
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

    def build_table(self) -> pa.Table:
        kinds = self._kinds
        return pa.table(
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

    chart_accounts: pa.Table
    journal_entry_kinds: pa.Table
    journal_entries: pa.Table
    postings: pa.Table
    balance_snapshots: pa.Table
    rollout_identity: pa.Table
    liability_ids: tuple[str, ...]
    # Source dict for fast `chart_account_id -> ChartAccount` lookup.
    # Built once at finalize and shared across materializations.
    chart_accounts_by_id: dict[str, ChartAccount]

    @classmethod
    def empty(cls) -> AccountingTrace:
        return cls(
            chart_accounts=_CHART_ACCOUNT_SCHEMA.empty_table(),
            journal_entry_kinds=_JOURNAL_ENTRY_KIND_SCHEMA.empty_table(),
            journal_entries=_JOURNAL_ENTRY_SCHEMA.empty_table(),
            postings=_POSTING_SCHEMA.empty_table(),
            balance_snapshots=_BALANCE_SNAPSHOT_SCHEMA.empty_table(),
            rollout_identity=_ROLLOUT_IDENTITY_SCHEMA.empty_table(),
            liability_ids=(),
            chart_accounts_by_id={},
        )

    def with_trajectory_identity(self, by_rollout: dict[int, dict[str, str]]) -> AccountingTrace:
        rollout_indexes = sorted(by_rollout.keys())
        rollout_identity = pa.table(
            {
                "rollout_index": pa.array(rollout_indexes, type=pa.int32()),
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
        then doing an Arrow `sort_indices` on the integer keys that map
        the same total order.
        """
        kinds_sorted_idx = _kind_canonical_permutation(self.journal_entry_kinds)
        if kinds_sorted_idx is not None:
            new_kinds = self.journal_entry_kinds.take(pa.array(kinds_sorted_idx, type=pa.int64()))
            # Remap kind_idx on journal_entries via the inverse permutation.
            inverse = np.empty(len(kinds_sorted_idx), dtype=np.int32)
            inverse[kinds_sorted_idx] = np.arange(len(kinds_sorted_idx), dtype=np.int32)
            old_kind_idx = self.journal_entries.column("kind_idx").to_numpy(zero_copy_only=False)
            remapped_kind_idx = inverse[old_kind_idx]
            new_journal_entries = self.journal_entries.set_column(
                self.journal_entries.schema.get_field_index("kind_idx"),
                "kind_idx",
                pa.array(remapped_kind_idx, type=pa.int32()),
            )
        else:
            new_kinds = self.journal_entry_kinds
            new_journal_entries = self.journal_entries

        je_sort = _pc_sort_indices(
            new_journal_entries,
            sort_keys=[("month_index", "ascending"), ("rollout_index", "ascending"), ("kind_idx", "ascending")],
        )
        je_sorted = new_journal_entries.take(je_sort)
        # Postings reference journal entries by index; remap them.
        je_perm = je_sort.to_numpy(zero_copy_only=False).astype(np.int64)
        je_inverse = np.empty(je_perm.size, dtype=np.int32)
        je_inverse[je_perm] = np.arange(je_perm.size, dtype=np.int32)
        old_je_idx = self.postings.column("journal_entry_idx").to_numpy(zero_copy_only=False)
        postings_remapped = self.postings.set_column(
            self.postings.schema.get_field_index("journal_entry_idx"),
            "journal_entry_idx",
            pa.array(je_inverse[old_je_idx], type=pa.int32()),
        )
        postings_sort = _pc_sort_indices(
            postings_remapped,
            sort_keys=[
                ("month_index", "ascending"),
                ("rollout_index", "ascending"),
                ("journal_entry_idx", "ascending"),
                ("posting_index", "ascending"),
            ],
        )
        postings_sorted = postings_remapped.take(postings_sort)

        snapshots_sort = _pc_sort_indices(
            self.balance_snapshots,
            sort_keys=[
                ("month_index", "ascending"),
                ("rollout_index", "ascending"),
                ("chart_account_idx", "ascending"),
            ],
        )
        snapshots_sorted = self.balance_snapshots.take(snapshots_sort)

        # Chart accounts canonical: by chart_account_id ascending. Remap
        # chart_account_idx on postings + balance snapshots accordingly.
        chart_perm = _pc_sort_indices(self.chart_accounts, sort_keys=[("chart_account_id", "ascending")])
        chart_perm_np = chart_perm.to_numpy(zero_copy_only=False).astype(np.int64)
        chart_inverse = np.empty(chart_perm_np.size, dtype=np.int32)
        chart_inverse[chart_perm_np] = np.arange(chart_perm_np.size, dtype=np.int32)
        new_chart_accounts = self.chart_accounts.take(chart_perm)
        if postings_sorted.num_rows:
            posting_acct = postings_sorted.column("chart_account_idx").to_numpy(zero_copy_only=False)
            postings_sorted = postings_sorted.set_column(
                postings_sorted.schema.get_field_index("chart_account_idx"),
                "chart_account_idx",
                pa.array(chart_inverse[posting_acct], type=pa.int32()),
            )
        if snapshots_sorted.num_rows:
            snap_acct = snapshots_sorted.column("chart_account_idx").to_numpy(zero_copy_only=False)
            snapshots_sorted = snapshots_sorted.set_column(
                snapshots_sorted.schema.get_field_index("chart_account_idx"),
                "chart_account_idx",
                pa.array(chart_inverse[snap_acct], type=pa.int32()),
            )

        new_chart_accounts_df = cast("pl.DataFrame", pl.from_arrow(new_chart_accounts))
        new_chart_accounts_by_id = {a.chart_account_id: a for a in _chart_accounts_from_pl(new_chart_accounts_df)}

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

    # Polars views ---------------------------------------------------------------
    #
    # The fact and dim tables are stored as `pa.Table` (so the builder can append
    # raw numpy chunks and we keep tight control over schemas). Joins and
    # filters live in polars: zero-copy conversion via `pl.from_arrow`, then
    # fluent expressions for the relational work that filter / aggregate /
    # materialize all reduce to.

    def _pl_postings(self) -> pl.DataFrame:
        return pl.from_arrow(self.postings)  # type: ignore[return-value]

    def _pl_journal_entries(self) -> pl.DataFrame:
        return pl.from_arrow(self.journal_entries)  # type: ignore[return-value]

    def _pl_journal_entry_kinds(self) -> pl.DataFrame:
        return pl.from_arrow(self.journal_entry_kinds)  # type: ignore[return-value]

    def _pl_chart_accounts(self) -> pl.DataFrame:
        return pl.from_arrow(self.chart_accounts)  # type: ignore[return-value]

    def _pl_balance_snapshots(self) -> pl.DataFrame:
        return pl.from_arrow(self.balance_snapshots)  # type: ignore[return-value]

    def _pl_rollout_identity(self) -> pl.DataFrame:
        return pl.from_arrow(self.rollout_identity)  # type: ignore[return-value]

    def _postings_joined(self) -> pl.DataFrame:
        """Postings with every column needed to materialize a Pydantic `Posting`.

        Joins postings → journal_entries (for the entry's rollout/month, used
        in id derivation) → journal_entry_kinds (for `cause_id_prefix`) →
        chart_accounts (for `chart_account_id`) → rollout_identity (for the
        four trajectory-identity strings).
        """
        return (
            self._pl_postings()
            .join(
                self._pl_journal_entries()
                .with_row_index("je_pos")
                .rename({"rollout_index": "je_rollout", "month_index": "je_month"}),
                left_on="journal_entry_idx",
                right_on="je_pos",
                how="inner",
            )
            .join(
                self._pl_journal_entry_kinds().with_row_index("kind_pos"),
                left_on="kind_idx",
                right_on="kind_pos",
                how="inner",
            )
            .join(
                self._pl_chart_accounts().with_row_index("acct_pos"),
                left_on="chart_account_idx",
                right_on="acct_pos",
                how="inner",
            )
            .join(self._pl_rollout_identity(), on="rollout_index", how="left")
        )

    def _journal_entries_joined(self) -> pl.DataFrame:
        """JournalEntries with kind + rollout identity columns attached."""
        return (
            self._pl_journal_entries()
            .join(
                self._pl_journal_entry_kinds().with_row_index("kind_pos"),
                left_on="kind_idx",
                right_on="kind_pos",
                how="inner",
            )
            .join(self._pl_rollout_identity(), on="rollout_index", how="left")
        )

    def _balance_snapshots_joined(self) -> pl.DataFrame:
        return (
            self._pl_balance_snapshots()
            .join(
                self._pl_chart_accounts().with_row_index("acct_pos"),
                left_on="chart_account_idx",
                right_on="acct_pos",
                how="inner",
            )
            .join(self._pl_rollout_identity(), on="rollout_index", how="left")
        )

    # Materialization to Pydantic ------------------------------------------------

    def chart_accounts_tuple(self) -> tuple[ChartAccount, ...]:
        return _chart_accounts_from_pl(self._pl_chart_accounts())

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
        df = self._pl_chart_accounts()
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
        matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
        if self.postings.num_rows == 0:
            return matrix

        df = self._postings_joined().filter(pl.col("role") == role.value)
        if side is not None:
            df = df.filter(pl.col("side") == _SIDE_TO_INT[side])
        if journal_entry_type is not None:
            df = df.filter(pl.col("journal_entry_type") == journal_entry_type.value)
        if df.height == 0:
            return matrix

        rollouts = df["rollout_index"].to_numpy()
        months = df["month_index"].to_numpy()
        amounts = df["amount_usd"].to_numpy()
        _scatter_amounts(matrix, rollouts, months, amounts, month_index, "posting")
        return matrix

    def balance_snapshot_amount_matrix(
        self, *, rollout_count: int, month_index: np.ndarray, role: ChartAccountRole
    ) -> np.ndarray:
        matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
        if self.balance_snapshots.num_rows == 0:
            return matrix

        df = self._balance_snapshots_joined().filter(pl.col("role") == role.value)
        if df.height == 0:
            return matrix

        rollouts = df["rollout_index"].to_numpy()
        months = df["month_index"].to_numpy()
        balances = df["balance_usd"].to_numpy()
        _scatter_amounts(matrix, rollouts, months, balances, month_index, "balance snapshot")
        return matrix

    def journal_entry_row(self, idx: int) -> JournalEntry:
        if idx < 0 or idx >= self.journal_entries.num_rows:
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


def _scatter_amounts(
    matrix: np.ndarray,
    rollouts: np.ndarray,
    months: np.ndarray,
    amounts: np.ndarray,
    month_index: np.ndarray,
    fact_label: str,
) -> None:
    """Fold per-row `(rollout, month_index, amount)` tuples into `matrix[rollout,
    month_position]` via `np.add.at`. `month_index` defines the result-horizon
    months in left-to-right order; rows referencing other months raise."""
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    try:
        month_positions = np.fromiter(
            (month_position_by_index[int(m)] for m in months), dtype=np.int64, count=months.size
        )
    except KeyError as exc:
        raise ValueError(f"{fact_label} has month outside result horizon: {exc.args[0]}") from exc
    np.add.at(matrix, (rollouts.astype(np.int64), month_positions), amounts)


def _kind_canonical_permutation(table: pa.Table) -> list[int] | None:
    """Order kinds by cause_id_prefix so the int sort matches today's string sort.

    Returns the permutation as a list[int] (length == num_rows) or None if
    the table is empty.
    """
    if table.num_rows == 0:
        return None
    cause_prefixes = table.column("cause_id_prefix").to_pylist()
    types = table.column("journal_entry_type").to_pylist()
    actors = [v or "" for v in table.column("actor_id").to_pylist()]
    policies = [v or "" for v in table.column("policy_id").to_pylist()]
    keys = [(cause_prefixes[i], types[i], actors[i], policies[i], i) for i in range(table.num_rows)]
    keys.sort()
    return [key[-1] for key in keys]


# Builder ---------------------------------------------------------------------


class AccountingTraceBuilder:
    """Vectorized builder for the columnar accounting trace.

    Maintains numpy-chunk buffers per fact-table column, interners for
    chart accounts and journal-entry kinds, and a small string interner
    for `liability_id`. `record_entry` and `record_snapshot` append in
    bulk per (rollout, month) batch; `finalize` concatenates the chunks
    and wraps them in `pa.Table`s for the returned `AccountingTrace`.
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
        chart_accounts_table = self._chart_accounts.build_table()
        journal_entry_kinds_table = self._journal_kinds.build_table()

        journal_entries_table = pa.table(
            {
                "rollout_index": self._je_rollout.to_array(),
                "month_index": self._je_month.to_array(),
                "kind_idx": self._je_kind.to_array(),
            },
            schema=_JOURNAL_ENTRY_SCHEMA,
        )

        po_lot_values, po_lot_valid = self._po_lot.to_arrays()
        po_liab_values, po_liab_valid = self._po_liab.to_arrays()
        postings_table = pa.table(
            {
                "rollout_index": self._po_rollout.to_array(),
                "month_index": self._po_month.to_array(),
                "journal_entry_idx": self._po_je_idx.to_array(),
                "posting_index": self._po_index.to_array(),
                "chart_account_idx": self._po_acct.to_array(),
                "side": self._po_side.to_array(),
                "amount_usd": self._po_amount.to_array(),
                "lot_idx": pa.array(po_lot_values, mask=~po_lot_valid, type=pa.int32()),
                "liability_idx": pa.array(po_liab_values, mask=~po_liab_valid, type=pa.int32()),
            },
            schema=_POSTING_SCHEMA,
        )

        bs_quantity_values, bs_quantity_valid = self._bs_quantity.to_arrays()
        balance_snapshots_table = pa.table(
            {
                "rollout_index": self._bs_rollout.to_array(),
                "month_index": self._bs_month.to_array(),
                "chart_account_idx": self._bs_acct.to_array(),
                "balance_usd": self._bs_balance.to_array(),
                "quantity": pa.array(bs_quantity_values, mask=~bs_quantity_valid, type=pa.float64()),
            },
            schema=_BALANCE_SNAPSHOT_SCHEMA,
        )

        return AccountingTrace(
            chart_accounts=chart_accounts_table,
            journal_entry_kinds=journal_entry_kinds_table,
            journal_entries=journal_entries_table,
            postings=postings_table,
            balance_snapshots=balance_snapshots_table,
            rollout_identity=_ROLLOUT_IDENTITY_SCHEMA.empty_table(),
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
    the indices are `pa.int32` foreign keys built by interners, so they
    can never reference unknown rows by construction.
    """
    n_entries = trace.journal_entries.num_rows
    n_postings = trace.postings.num_rows
    if n_entries == 0 and n_postings == 0:
        return

    if n_entries > 0:
        je_idx = trace.postings.column("journal_entry_idx").to_numpy(zero_copy_only=False)
        has_posting = np.zeros(n_entries, dtype=bool)
        if je_idx.size:
            has_posting[je_idx] = True
        if not has_posting.all():
            missing = np.flatnonzero(~has_posting)
            raise AccountingValidationError(
                f"{missing.size} journal entry/entries have no postings; first idx: {int(missing[0])}"
            )

    if n_postings > 0:
        je_idx = trace.postings.column("journal_entry_idx").to_numpy(zero_copy_only=False)
        sides = trace.postings.column("side").to_numpy(zero_copy_only=False)
        amounts = trace.postings.column("amount_usd").to_numpy(zero_copy_only=False)
        signed = np.where(sides == 0, amounts, -amounts)
        net = np.zeros(n_entries, dtype=np.float64)
        np.add.at(net, je_idx, signed)
        bad = np.flatnonzero(np.abs(net) > tolerance_usd)
        if bad.size > 0:
            first_bad = int(bad[0])
            raise AccountingValidationError(
                f"{bad.size} journal entry/entries unbalanced; "
                f"first idx={first_bad} net debits-credits={net[first_bad]:.4f}"
            )


__all__ = ["AccountingTrace", "AccountingTraceBuilder", "validate_trace"]
