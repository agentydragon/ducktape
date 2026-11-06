"""Tool policies and loop decisions used by Agent handlers."""

# TODO: Consider optional debug-only injection knobs for synthetic transcript items or tool outputs; default off.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage


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
class InjectItems:
    """Append items to the transcript and queue FunctionCallItems for execution.

    Skip sampling after injection; handlers run again on the next iteration.
    """

    items: Sequence[SystemMessage | UserMessage | FunctionCallItem]


@dataclass(frozen=True)
class Abort:
    pass


@dataclass(frozen=True)
class Compact:
    """Compact earlier transcript items and keep the final keep_recent_turns items verbatim.

    The next iteration uses the configured tool policy.
    """

    keep_recent_turns: int = 10


type LoopDecision = NoAction | InjectItems | Abort | Compact
