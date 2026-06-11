"""ID system for properties/grading.

Provides strongly-typed IDs with namespace separation:
- BaseIssueID: Un-namespaced IDs (used in specimens, critique)
- InputIssueID: NewType wrapper for type safety

All IDs are Pydantic-validated. NewTypes provide compile-time type safety
(mypy distinguishes them) while remaining strings at runtime (work as JSON dict keys).
"""

from typing import Annotated, NewType

from pydantic import PlainSerializer, StringConstraints

# Preserves string value unchanged during serialization
_STR_IDENTITY_SERIALIZER = PlainSerializer(lambda x: x, return_type=str, when_used="json")

# Base issue ID type (used in specimens, critique definitions)
# Pattern: lowercase alphanumeric, underscore, hyphen only (5-40 characters)
type BaseIssueID = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_-]+$", min_length=5, max_length=40), _STR_IDENTITY_SERIALIZER
]


# Base validated snapshot slug type (internal)
# Pattern: {project}/{date-sequence}
#   - project: lowercase alphanumeric, underscore, hyphen (e.g., "ducktape", "crush", "misc")
#   - date-sequence: typically YYYY-MM-DD-NN or YYYY-MM-DD-name (e.g., "2025-11-26-00", "2025-08-30-internal_db")
# Constraint: EXACTLY ONE SLASH for consistent directory depth in runs/
#
type _SnapshotSlugBase = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9_-]+/[a-z0-9_-]+$",
        min_length=3,  # Minimum: "a/b"
        max_length=100,  # Reasonable upper bound
    ),
    _STR_IDENTITY_SERIALIZER,
]

# Public snapshot slug type (NewType for nominal type safety)
# Compile-time distinct from bare str, runtime is validated _SnapshotSlugBase string
SnapshotSlug = NewType("SnapshotSlug", _SnapshotSlugBase)
"""Snapshot slug ID. Compile-time distinct from str, runtime is validated string."""

# TODO: SnapshotSlug uses NewType which doesn't validate on construction (SnapshotSlug("foo")
# just returns "foo" without validation). This is inconsistent with newer patterns like
# MCPMountPrefix which is a str subclass with validating __new__. Consider migrating to
# the validating constructor pattern for consistency.


# NewType creates nominal types for mypy (compile-time type safety)
# At runtime, these are just BaseIssueID strings (work as JSON dict keys)
# Type is implied by position in data structure

InputIssueID = NewType("InputIssueID", BaseIssueID)
"""Input critique ID. Compile-time distinct from other ID types, runtime is BaseIssueID string."""

DefinitionId = NewType("DefinitionId", str)
"""Agent definition ID. Compile-time distinct from str, runtime is string."""


def split_snapshot_slug(slug: SnapshotSlug) -> tuple[str, str]:
    """Split snapshot slug into repo and version components.

    Args:
        slug: Snapshot slug like "ducktape/2025-11-26-00"

    Returns:
        Tuple of (repo, version) e.g., ('ducktape', '2025-11-26-00')

    Example:
        >>> repo, version = split_snapshot_slug(SnapshotSlug("ducktape/2025-11-26-00"))
        >>> repo
        'ducktape'
        >>> version
        '2025-11-26-00'
    """
    parts = str(slug).split("/", 1)
    return parts[0], parts[1]
