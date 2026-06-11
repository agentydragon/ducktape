"""Custom Typer/Click parameter types for props CLI."""

from __future__ import annotations

import click
from pydantic import TypeAdapter, ValidationError

from props.core.ids import _SnapshotSlugBase


class SnapshotSlugParamType(click.ParamType):
    """Click parameter type for SnapshotSlug validation."""

    name = "snapshot_slug"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        """Validate snapshot slug format."""
        try:
            # Use Pydantic validation from the base type
            adapter = TypeAdapter(_SnapshotSlugBase)
            adapter.validate_python(value)
            return value
        except ValidationError as e:
            self.fail(f"Invalid snapshot slug '{value}': {e}", param, ctx)
            raise AssertionError("unreachable")


class DefinitionIdParamType(click.ParamType):
    """Click parameter type for DefinitionId (simple string passthrough)."""

    name = "definition_id"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        """Pass through string value (DefinitionId is just a NewType of str)."""
        return value


# Singleton instances
SNAPSHOT_SLUG = SnapshotSlugParamType()
DEFINITION_ID = DefinitionIdParamType()
