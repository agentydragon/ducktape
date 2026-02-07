"""Custom Typer/Click parameter types for props CLI."""

from __future__ import annotations

import click
from pydantic import TypeAdapter, ValidationError

from props.core.ids import _SnapshotSlugBase


class SnapshotSlugParamType(click.ParamType):
    name = "snapshot_slug"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        try:
            adapter = TypeAdapter(_SnapshotSlugBase)
            adapter.validate_python(value)
            return value
        except ValidationError as e:
            self.fail(f"Invalid snapshot slug '{value}': {e}", param, ctx)
            raise AssertionError("unreachable")


# Singleton instance
SNAPSHOT_SLUG = SnapshotSlugParamType()
