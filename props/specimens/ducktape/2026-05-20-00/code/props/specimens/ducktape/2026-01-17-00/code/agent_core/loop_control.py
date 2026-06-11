"""Generic loop-control primitives for Agent agents.

This module intentionally avoids application-specific concerns. It exposes
minimal algebraic types used by handlers to influence the agent loop each
sampling step (e.g., requiring a tool call for the next sampling or aborting
the turn).

Policies and decisions are expressed as algebraic types to keep them disjoint
and easy to compose.

Notes / Future work (TODOs):
- Add a RequireSpecific(names: tuple[str, ...]) policy to constrain the next tool
  choice to a known subset once a concrete use case appears.
- Consider optional injection knobs (for debugging only), e.g., synthesizing
  transcript messages or function-call outputs; default off to keep core clean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

# ---------------------------------------------------------------------------
# Tool policy algebraic types (what the model is allowed/required to do next)
# ---------------------------------------------------------------------------


class ToolPolicy:
    """Base class for tool-choice policy."""


@dataclass(frozen=True)
class AllowAnyToolOrTextMessage(ToolPolicy):
    """Let the model decide whether to call a tool or not for the next sample."""


@dataclass(frozen=True)
class RequireAnyTool(ToolPolicy):
    """Require the model to call at least one tool for the next sample."""


@dataclass(frozen=True)
class ForbidAllTools(ToolPolicy):
    """Disallow tool calls for the next sample (rarely useful)."""


# Constrained-required policy: require one of specific tool names
# Note: Names should match the function names exposed to the model (e.g.,
# use build_mcp_function to compose "prompt_feedback" / "propose_prompt").
@dataclass(frozen=True)
class RequireSpecific(ToolPolicy):
    names: tuple[str, ...]


# ---------------------------------------------------------------------------
# Loop decision algebraic types (continue vs abort the turn)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoAction:
    """Handler has no opinion; continue to next handler.

    When returned by a handler: "I defer, check the next handler"
    When all handlers defer: "Sample the LLM normally"

    This replaces both NoLoopDecision and Continue, which had identical
    semantics in the sequential evaluation model.
    """


@dataclass(frozen=True)
class InjectItems:
    """Inject items into the agent loop, then skip sampling.

    Supported item types:
    - SystemMessage: inject system guidance (e.g., warnings, constraints)
    - UserMessage: inject user input (e.g., notifications)
    - FunctionCallItem: inject tool calls (e.g., bootstrap)

    All items are appended to transcript. FunctionCallItem objects are
    added to pending_function_calls for execution. After injection,
    the loop continues without sampling (handlers run again next iteration).
    """

    items: Sequence[SystemMessage | UserMessage | FunctionCallItem]


@dataclass(frozen=True)
class Abort:
    pass


@dataclass(frozen=True)
class Compact:
    """Signal that transcript should be compacted before continuing.

    The agent will compact the transcript (keeping keep_recent_turns items),
    then continue with the agent's configured tool_policy.

    Note: ReasoningItem blocks are never preserved in the recent region,
    as they cannot be reused outside their original response context.
    """

    keep_recent_turns: int = 10


# Union type for loop decisions (for static type checking)
type LoopDecision = NoAction | InjectItems | Abort | Compact
