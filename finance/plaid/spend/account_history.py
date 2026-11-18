"""Dated opening and transaction coverage evidence shared across Spend reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccountOpening(BaseModel):
    """Dated evidence of account existence, independently of imported history."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    earliest: date
    latest: date
    source: str = Field(min_length=1, pattern=r"\S")
    as_of: date

    @model_validator(mode="after")
    def _ordered_bounds(self) -> AccountOpening:
        if self.earliest > self.latest:
            raise ValueError("account opening bounds must be ordered")
        return self


class DocumentedHistory(BaseModel):
    """Explicit assertion of complete history from a date, with provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    complete_from: date
    source: str = Field(min_length=1, pattern=r"\S")
    as_of: date

    @model_validator(mode="after")
    def _dated_coverage(self) -> DocumentedHistory:
        if self.complete_from > self.as_of:
            raise ValueError("history coverage cannot start after its evidence date")
        return self


class AccountHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    opening: AccountOpening | None = None
    coverage: DocumentedHistory | None = None

    @model_validator(mode="after")
    def _has_evidence(self) -> AccountHistory:
        if self.opening is None and self.coverage is None:
            raise ValueError("account history requires opening or coverage evidence")
        return self


@dataclass(frozen=True)
class AccountCoverage:
    first_recorded: date | None
    evidence: AccountHistory | None

    def covers(self, day: date) -> bool:
        if self.evidence is not None:
            if self.evidence.opening is not None and day < self.evidence.opening.earliest:
                return True
            if self.evidence.coverage is not None:
                return day >= self.evidence.coverage.complete_from
        return self.first_recorded is not None and day >= self.first_recorded
