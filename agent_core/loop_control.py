"""Tool policies and loop decisions used by Agent handlers."""

from __future__ import annotations

from dataclasses import dataclass


class ToolPolicy:
    """Base type for tool-choice policies."""


@dataclass(frozen=True)
class AllowAnyToolOrTextMessage(ToolPolicy):
    """Let the model choose between tool calls and ordinary text."""


@dataclass(frozen=True)
class RequireAnyTool(ToolPolicy):
    """Require at least one tool call on the next sample."""


@dataclass(frozen=True)
class NoAction:
    """Defer to the next handler; if all defer, sample the model normally."""


@dataclass(frozen=True)
class Abort:
    pass


type LoopDecision = NoAction | Abort
