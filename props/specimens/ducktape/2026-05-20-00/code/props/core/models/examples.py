"""Example specification models (Pydantic discriminated union).

ExampleSpec is the Pydantic representation of database Example rows.
It defines which snapshot and scope (whole-snapshot or file-set) to evaluate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.core.ids import SnapshotSlug


class ExampleKind(StrEnum):
    """Discriminator values for ExampleSpec union.

    Matches the PostgreSQL example_kind_enum type values exactly.
    """

    WHOLE_SNAPSHOT = "whole_snapshot"
    FILE_SET = "file_set"


class SingleFileSetExample(OpenAIStrictModeBaseModel):
    """Review files from a specific file set.

    Files are resolved via files_hash FK join in the database.
    Corresponds to per-file training examples.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[ExampleKind.FILE_SET] = ExampleKind.FILE_SET
    snapshot_slug: SnapshotSlug = Field(description="Snapshot to evaluate")
    files_hash: str = Field(description="File set hash (MD5 of sorted file paths)")

    def __str__(self) -> str:
        """Return string representation for display."""
        return f"fileset:{self.files_hash[:8]}@{self.snapshot_slug}"


class WholeSnapshotExample(OpenAIStrictModeBaseModel):
    """Review all files in the snapshot.

    Corresponds to whole-snapshot evaluation examples.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[ExampleKind.WHOLE_SNAPSHOT] = ExampleKind.WHOLE_SNAPSHOT
    snapshot_slug: SnapshotSlug = Field(description="Snapshot to evaluate")

    def __str__(self) -> str:
        """Return string representation for display."""
        return f"whole@{self.snapshot_slug}"


ExampleSpec = Annotated[WholeSnapshotExample | SingleFileSetExample, Field(discriminator="kind")]


__all__ = ["ExampleKind", "ExampleSpec", "SingleFileSetExample", "WholeSnapshotExample"]
