"""Sole owner of how income reaches a row of the year-to-date income tensor.

A row — a *bucket* — is a `(tax profile, income source)` pair flattened to one index, so
that a jurisdiction can include an agent's wages and exclude the same agent's municipal
coupon. Flattening rather than adding an array dimension is what lets the engine keep its
existing row-scatter machinery: accruing categorized income is "write a different row
number".

Everything that writes income, deducts from it, or reads it back goes through here. That
is the point of the module. The alternative — each site recomputing `profile * n + source`
— is what this replaced, and it was quietly wrong in three places: with one tax profile,
`profile` and `profile * n + ordinary` are both 0, so profile-indexed arithmetic against a
bucket-indexed tensor produced correct numbers for every existing scenario and would have
started corrupting them at the second taxed agent.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.compiler.helpers import NO_CODE
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome, TransferIncomeCategory


def income_source_sort_key(category: TransferIncomeCategory) -> tuple[int, str]:
    """Deterministic order: ordinary first, then interest by issuer, corporate last.

    Bucket indices are baked into the jitted program's static structure, so an unstable
    order would re-trace on every other compile.
    """

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return (1, issuer) if issuer is not None else (2, "")
    return (0, "")


@dataclass(frozen=True)
class IncomeBuckets:
    """The (profile, source) → row mapping, and the only thing that knows the arithmetic."""

    source_ids: tuple[TransferIncomeCategory, ...]
    profile_count: int

    @classmethod
    def for_sources(cls, sources: set[TransferIncomeCategory], *, profile_count: int) -> IncomeBuckets:
        """Ordinary is always present, so a scenario with no interest keeps exactly the
        one-row-per-profile tensor it had before this axis existed."""

        return cls(
            source_ids=tuple(sorted({OrdinaryIncome(), *sources}, key=income_source_sort_key)),
            profile_count=profile_count,
        )

    @property
    def row_count(self) -> int:
        return self.profile_count * len(self.source_ids)

    def bucket(self, profile_index: int, category: TransferIncomeCategory) -> int:
        """Row for one agent's income of one kind. `NO_CODE` in, `NO_CODE` out — an untaxed
        recipient routes to `_scatter_rows`'s dump row and contributes nothing."""

        if profile_index == NO_CODE:
            return NO_CODE
        return profile_index * len(self.source_ids) + self.source_ids.index(category)

    def ordinary_bucket(self, profile_index: int) -> int:
        """Row anything untagged lands in — deductions, capital-loss offsets, the bracket walk."""

        return self.bucket(profile_index, OrdinaryIncome())

    def ordinary_rows(self, profile_indices: NDArray[np.int64]) -> NDArray[np.int64]:
        """Vectorized `ordinary_bucket`, preserving `NO_CODE` sentinels.

        For the engine's scatter targets, which are arrays of profile indices built before
        this axis existed and would otherwise write into another profile's source rows.
        """

        indices = np.asarray(profile_indices, dtype=np.int64)
        offset = self.source_ids.index(OrdinaryIncome())
        return np.where(indices == NO_CODE, NO_CODE, indices * len(self.source_ids) + offset)

    def source_wire_ids(self) -> tuple[str, ...]:
        """Human-readable per-source labels, for the decoded read model only."""

        return tuple(_wire_id(source) for source in self.source_ids)


def _wire_id(category: TransferIncomeCategory) -> str:
    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return f"interest:{issuer if issuer is not None else 'corporate'}"
    return "ordinary"
