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

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import numpy as np
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
_pc_sort_indices = pc.sort_indices  # type: ignore[attr-defined]
_pc_equal = pc.equal  # type: ignore[attr-defined]
_pc_is_in = pc.is_in  # type: ignore[attr-defined]


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

        new_chart_accounts_by_id = {a.chart_account_id: a for a in _chart_accounts_to_pydantic(new_chart_accounts)}

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

    # Materialization to Pydantic ------------------------------------------------

    def chart_accounts_tuple(self) -> tuple[ChartAccount, ...]:
        return tuple(_chart_accounts_to_pydantic(self.chart_accounts))

    def journal_entries_tuple(self) -> tuple[JournalEntry, ...]:
        return tuple(self._iter_journal_entries(self.journal_entries))

    def postings_tuple(self) -> tuple[Posting, ...]:
        return tuple(self._iter_postings(self.postings))

    def balance_snapshots_tuple(self) -> tuple[BalanceSnapshot, ...]:
        return tuple(self._iter_balance_snapshots(self.balance_snapshots))

    # Filters --------------------------------------------------------------------

    def filter_postings(
        self, *, rollout: int | None = None, side: PostingSide | None = None, role: ChartAccountRole | None = None
    ) -> tuple[Posting, ...]:
        table = self.postings
        if rollout is not None:
            table = table.filter(_pc_equal(table["rollout_index"], rollout))
        if side is not None:
            table = table.filter(_pc_equal(table["side"], _SIDE_TO_INT[side]))
        if role is not None:
            account_indices = _chart_account_indices_with_role(self.chart_accounts, role)
            table = table.filter(
                _pc_is_in(table["chart_account_idx"], value_set=pa.array(account_indices, type=pa.int32()))
            )
        return tuple(self._iter_postings(table))

    def filter_journal_entries(
        self, *, rollout: int | None = None, journal_entry_type: JournalEntryType | None = None
    ) -> tuple[JournalEntry, ...]:
        table = self.journal_entries
        if journal_entry_type is not None:
            kind_indices = _kind_indices_with_type(self.journal_entry_kinds, journal_entry_type)
            table = table.filter(_pc_is_in(table["kind_idx"], value_set=pa.array(kind_indices, type=pa.int32())))
        if rollout is not None:
            table = table.filter(_pc_equal(table["rollout_index"], rollout))
        return tuple(self._iter_journal_entries(table))

    def filter_balance_snapshots(
        self, *, rollout: int | None = None, role: ChartAccountRole | None = None
    ) -> tuple[BalanceSnapshot, ...]:
        table = self.balance_snapshots
        if role is not None:
            account_indices = _chart_account_indices_with_role(self.chart_accounts, role)
            table = table.filter(
                _pc_is_in(table["chart_account_idx"], value_set=pa.array(account_indices, type=pa.int32()))
            )
        if rollout is not None:
            table = table.filter(_pc_equal(table["rollout_index"], rollout))
        return tuple(self._iter_balance_snapshots(table))

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
        """Sum posting amounts by (rollout, month_position) into a dense matrix.

        Equivalent to today's per-`Posting` loop in
        `augur/core/scenario_engine.py:_posting_amount_matrix`, implemented
        columnarly: one boolean mask over the postings table per filter,
        one `np.add.at` for the scatter. Mostly used by the engine's
        post-simulation aggregations and by tests that want to roll up
        postings by (rollout, month) without materializing Pydantic models.
        """
        matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
        if self.postings.num_rows == 0:
            return matrix

        posting_acct = self.postings.column("chart_account_idx").to_numpy(zero_copy_only=False)
        acct_role_col = self.chart_accounts.column("role").to_numpy(zero_copy_only=False)
        role_acct_idxs = np.flatnonzero(acct_role_col == role.value)
        if role_acct_idxs.size == 0:
            return matrix
        mask = np.isin(posting_acct, role_acct_idxs)

        if side is not None:
            sides = self.postings.column("side").to_numpy(zero_copy_only=False)
            side_int = 0 if side is PostingSide.DEBIT else 1
            mask &= sides == side_int

        if journal_entry_type is not None:
            kind_type_col = self.journal_entry_kinds.column("journal_entry_type").to_numpy(zero_copy_only=False)
            kind_idxs_with_type = np.flatnonzero(kind_type_col == journal_entry_type.value)
            if kind_idxs_with_type.size == 0:
                return matrix
            je_kind_col = self.journal_entries.column("kind_idx").to_numpy(zero_copy_only=False)
            je_idxs_with_type = np.flatnonzero(np.isin(je_kind_col, kind_idxs_with_type))
            posting_je = self.postings.column("journal_entry_idx").to_numpy(zero_copy_only=False)
            mask &= np.isin(posting_je, je_idxs_with_type)

        if not mask.any():
            return matrix

        rollouts = self.postings.column("rollout_index").to_numpy(zero_copy_only=False)[mask]
        months = self.postings.column("month_index").to_numpy(zero_copy_only=False)[mask]
        amounts = self.postings.column("amount_usd").to_numpy(zero_copy_only=False)[mask]

        month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
        try:
            month_positions = np.fromiter(
                (month_position_by_index[int(m)] for m in months), dtype=np.int64, count=months.size
            )
        except KeyError as exc:
            raise ValueError(f"posting has month outside result horizon: {exc.args[0]}") from exc

        np.add.at(matrix, (rollouts.astype(np.int64), month_positions), amounts)
        return matrix

    def balance_snapshot_amount_matrix(
        self, *, rollout_count: int, month_index: np.ndarray, role: ChartAccountRole
    ) -> np.ndarray:
        """Sum balance snapshot amounts by (rollout, month_position). Columnar
        equivalent of today's per-`BalanceSnapshot` loop in
        `augur/core/scenario_engine.py:_balance_snapshot_amount_matrix`.
        """
        matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
        if self.balance_snapshots.num_rows == 0:
            return matrix

        snap_acct = self.balance_snapshots.column("chart_account_idx").to_numpy(zero_copy_only=False)
        acct_role_col = self.chart_accounts.column("role").to_numpy(zero_copy_only=False)
        role_acct_idxs = np.flatnonzero(acct_role_col == role.value)
        if role_acct_idxs.size == 0:
            return matrix
        mask = np.isin(snap_acct, role_acct_idxs)
        if not mask.any():
            return matrix

        rollouts = self.balance_snapshots.column("rollout_index").to_numpy(zero_copy_only=False)[mask]
        months = self.balance_snapshots.column("month_index").to_numpy(zero_copy_only=False)[mask]
        balances = self.balance_snapshots.column("balance_usd").to_numpy(zero_copy_only=False)[mask]

        month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
        try:
            month_positions = np.fromiter(
                (month_position_by_index[int(m)] for m in months), dtype=np.int64, count=months.size
            )
        except KeyError as exc:
            raise ValueError(f"balance snapshot has month outside result horizon: {exc.args[0]}") from exc

        np.add.at(matrix, (rollouts.astype(np.int64), month_positions), balances)
        return matrix

    def filter_chart_accounts(self, *, role: ChartAccountRole | None = None) -> tuple[ChartAccount, ...]:
        if role is None:
            return self.chart_accounts_tuple()
        table = self.chart_accounts.filter(_pc_equal(self.chart_accounts["role"], role.value))
        return tuple(_chart_accounts_to_pydantic(table))

    # Internal materialization helpers ------------------------------------------

    def _iter_journal_entries(self, table: pa.Table) -> Iterator[JournalEntry]:
        if table.num_rows == 0:
            return
        rollouts = table.column("rollout_index").to_pylist()
        months = table.column("month_index").to_pylist()
        kinds = table.column("kind_idx").to_pylist()
        identity = _rollout_identity_lookup(self.rollout_identity)
        kind_rows = _journal_entry_kind_rows(self.journal_entry_kinds)
        for rollout, month, kind_idx in zip(rollouts, months, kinds, strict=True):
            kind = kind_rows[kind_idx]
            yield _build_journal_entry(
                rollout_index=rollout, month_index=month, kind=kind, identity=identity.get(rollout, _EMPTY_IDENTITY)
            )

    def _iter_postings(self, table: pa.Table) -> Iterator[Posting]:
        if table.num_rows == 0:
            return
        rollouts = table.column("rollout_index").to_pylist()
        months = table.column("month_index").to_pylist()
        je_idxs = table.column("journal_entry_idx").to_pylist()
        post_idxs = table.column("posting_index").to_pylist()
        acct_idxs = table.column("chart_account_idx").to_pylist()
        sides = table.column("side").to_pylist()
        amounts = table.column("amount_usd").to_pylist()
        liab_idxs = table.column("liability_idx").to_pylist()
        identity = _rollout_identity_lookup(self.rollout_identity)
        kind_rows = _journal_entry_kind_rows(self.journal_entry_kinds)
        je_kind_idx = self.journal_entries.column("kind_idx").to_pylist() if self.journal_entries.num_rows else []
        je_rollout = self.journal_entries.column("rollout_index").to_pylist() if self.journal_entries.num_rows else []
        je_month = self.journal_entries.column("month_index").to_pylist() if self.journal_entries.num_rows else []
        chart_ids = self.chart_accounts.column("chart_account_id").to_pylist()
        for i in range(table.num_rows):
            rollout = rollouts[i]
            month = months[i]
            je_idx = je_idxs[i]
            posting_index = post_idxs[i]
            kind = kind_rows[je_kind_idx[je_idx]]
            side = _INT_TO_SIDE[sides[i]]
            chart_account_id_value = chart_ids[acct_idxs[i]]
            journal_entry_id = _trace_row_id(
                kind.cause_id_prefix, rollout_index=je_rollout[je_idx], month_index=je_month[je_idx]
            )
            posting_id = f"{journal_entry_id}:posting:{posting_index}:{side.value}"
            id_fields = identity.get(rollout, _EMPTY_IDENTITY)
            liab_idx = liab_idxs[i]
            yield Posting(
                posting_id=posting_id,
                journal_entry_id=journal_entry_id,
                rollout_index=rollout,
                month_index=month,
                chart_account_id=chart_account_id_value,
                side=side,
                amount_usd=amounts[i],
                liability_id=self.liability_ids[liab_idx] if liab_idx is not None else None,
                path_set_id=id_fields.get("path_set_id"),
                exogenous_path_id=id_fields.get("exogenous_path_id"),
                scenario_input_id=id_fields.get("scenario_input_id"),
                projection_trajectory_id=id_fields.get("projection_trajectory_id"),
            )

    def _iter_balance_snapshots(self, table: pa.Table) -> Iterator[BalanceSnapshot]:
        if table.num_rows == 0:
            return
        rollouts = table.column("rollout_index").to_pylist()
        months = table.column("month_index").to_pylist()
        acct_idxs = table.column("chart_account_idx").to_pylist()
        balances = table.column("balance_usd").to_pylist()
        quantities = table.column("quantity").to_pylist()
        identity = _rollout_identity_lookup(self.rollout_identity)
        chart_ids = self.chart_accounts.column("chart_account_id").to_pylist()
        for i in range(table.num_rows):
            rollout = rollouts[i]
            id_fields = identity.get(rollout, _EMPTY_IDENTITY)
            yield BalanceSnapshot(
                rollout_index=rollout,
                month_index=months[i],
                chart_account_id=chart_ids[acct_idxs[i]],
                balance_usd=balances[i],
                quantity=quantities[i],
                path_set_id=id_fields.get("path_set_id"),
                exogenous_path_id=id_fields.get("exogenous_path_id"),
                scenario_input_id=id_fields.get("scenario_input_id"),
                projection_trajectory_id=id_fields.get("projection_trajectory_id"),
            )

    def journal_entry_row(self, idx: int) -> JournalEntry:
        if idx < 0 or idx >= self.journal_entries.num_rows:
            raise IndexError(idx)
        return next(self._iter_journal_entries(self.journal_entries.slice(idx, 1)))


_EMPTY_IDENTITY: dict[str, str | None] = {
    "path_set_id": None,
    "exogenous_path_id": None,
    "scenario_input_id": None,
    "projection_trajectory_id": None,
}


def _rollout_identity_lookup(table: pa.Table) -> dict[int, dict[str, str | None]]:
    if table.num_rows == 0:
        return {}
    rollouts = table.column("rollout_index").to_pylist()
    keys = ("path_set_id", "exogenous_path_id", "scenario_input_id", "projection_trajectory_id")
    columns = {key: table.column(key).to_pylist() for key in keys}
    return {rollout: {key: columns[key][i] for key in keys} for i, rollout in enumerate(rollouts)}


def _journal_entry_kind_rows(table: pa.Table) -> list[_JournalEntryKindRow]:
    if table.num_rows == 0:
        return []
    cols = {name: table.column(name).to_pylist() for name in table.schema.names}
    return [
        _JournalEntryKindRow(
            journal_entry_type=JournalEntryType(cols["journal_entry_type"][i]),
            cause_type=AccountingCauseType(cols["cause_type"][i]),
            cause_id_prefix=cols["cause_id_prefix"][i],
            actor_id=cols["actor_id"][i],
            policy_id=cols["policy_id"][i],
            event_id=cols["event_id"][i],
            obligation_id_prefix=cols["obligation_id_prefix"][i],
            description=cols["description"][i],
        )
        for i in range(table.num_rows)
    ]


def _build_journal_entry(
    *, rollout_index: int, month_index: int, kind: _JournalEntryKindRow, identity: dict[str, str | None]
) -> JournalEntry:
    journal_entry_id = _trace_row_id(kind.cause_id_prefix, rollout_index=rollout_index, month_index=month_index)
    obligation_id = (
        _trace_row_id(kind.obligation_id_prefix, rollout_index=rollout_index, month_index=month_index)
        if kind.obligation_id_prefix is not None
        else None
    )
    return JournalEntry(
        journal_entry_id=journal_entry_id,
        rollout_index=rollout_index,
        month_index=month_index,
        journal_entry_type=kind.journal_entry_type,
        actor_id=kind.actor_id,
        policy_id=kind.policy_id,
        event_id=kind.event_id,
        obligation_id=obligation_id,
        description=kind.description,
        cause=AccountingCause(
            cause_type=kind.cause_type,
            cause_id=journal_entry_id,
            policy_id=kind.policy_id,
            event_id=kind.event_id,
            obligation_id=obligation_id,
        ),
        path_set_id=identity.get("path_set_id"),
        exogenous_path_id=identity.get("exogenous_path_id"),
        scenario_input_id=identity.get("scenario_input_id"),
        projection_trajectory_id=identity.get("projection_trajectory_id"),
    )


def _chart_accounts_to_pydantic(table: pa.Table) -> Iterator[ChartAccount]:
    if table.num_rows == 0:
        return
    cols = {name: table.column(name).to_pylist() for name in table.schema.names}
    for i in range(table.num_rows):
        yield ChartAccount(
            chart_account_id=cols["chart_account_id"][i],
            account_type=ChartAccountType(cols["account_type"][i]),
            role=ChartAccountRole(cols["role"][i]),
            actor_id=cols["actor_id"][i],
            label=cols["label"][i],
            source_account_id=cols["source_account_id"][i],
            source_asset_id=cols["source_asset_id"][i],
            liability_id=cols["liability_id"][i],
            property_id=cols["property_id"][i],
            counterparty_actor_id=cols["counterparty_actor_id"][i],
        )


def _chart_account_indices_with_role(table: pa.Table, role: ChartAccountRole) -> list[int]:
    roles = table.column("role").to_pylist()
    return [i for i, r in enumerate(roles) if r == role.value]


def _kind_indices_with_type(table: pa.Table, journal_entry_type: JournalEntryType) -> list[int]:
    types = table.column("journal_entry_type").to_pylist()
    return [i for i, t in enumerate(types) if t == journal_entry_type.value]


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
