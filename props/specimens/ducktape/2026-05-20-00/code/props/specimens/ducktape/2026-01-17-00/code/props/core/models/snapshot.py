from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from props.core.ids import SnapshotSlug
from props.core.splits import Split


class GitSource(BaseModel):
    vcs: Literal["git"]
    url: str
    commit: str  # Full commit SHA for cache validation
    ref: str | None = None  # Optional tag/branch name for convenience


class GitHubSource(BaseModel):
    vcs: Literal["github"]
    org: str
    repo: str
    ref: str


class LocalSource(BaseModel):
    vcs: Literal["local"]
    root: str = "."


Source = Annotated[GitSource | GitHubSource | LocalSource, Field(discriminator="vcs")]


class BundleFilter(BaseModel):
    """Historical metadata about snapshot capture (optional, for reference only).

    Records the source commit and filters that were used when the snapshot was captured.
    Not used for runtime operations - preserved for provenance and reproducibility.

    Gitignore-style patterns:
    - Trailing slash means directory (e.g., "web/" excludes the web directory)
    - No wildcards needed for "everything under" (e.g., "adgn/" includes all of adgn/)
    """

    source_commit: str  # Full commit SHA in the original source repository
    include: list[str] | None = None
    exclude: list[str] | None = None


class SnapshotDoc(BaseModel):
    """Snapshot document: source, split assignment, and optional historical metadata.

    This is the schema for per-snapshot manifest.yaml files.

    Issues are stored in the database (TruePositive/FalsePositive ORM tables).
    The issue YAML files are synced to the database once via `props db sync`.

    Bundle is optional historical metadata - records the source commit and filters used
    during capture for provenance. Not used for runtime hydration operations.
    Split is required - every snapshot must be assigned to train/valid/test.
    """

    source: Source
    split: Split = Field(description="Train/valid/test split assignment for this snapshot")
    bundle: BundleFilter | None = None
    model_config = ConfigDict(extra="forbid")


__all__ = ["BundleFilter", "GitHubSource", "GitSource", "LocalSource", "SnapshotDoc", "SnapshotSlug", "Source"]
