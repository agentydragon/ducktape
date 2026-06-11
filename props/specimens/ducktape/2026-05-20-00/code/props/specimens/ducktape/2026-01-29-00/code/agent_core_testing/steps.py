"""Shared test utilities for MCP tool testing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EmptyArgs(BaseModel):
    """Empty arguments for zero-parameter MCP tools."""

    model_config = ConfigDict(extra="forbid")
