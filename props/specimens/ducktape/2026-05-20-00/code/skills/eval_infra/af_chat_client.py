"""Build a Microsoft Agent Framework chat client for an `(api, model)` pair.

`function_invocation_configuration` (e.g. `{"max_iterations": 200}`) is
applied at construction time so callers don't have to mutate the client
afterwards. AF chat clients have no `close()` / async context manager
hooks (verified against the Anthropic / OpenAI / Base classes); the
underlying SDK clients are released by Python's GC at process exit.

`strict_tools=True` engages Anthropic's grammar-constrained sampling for
tool calls (https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use):
``strict: true`` is set on every custom tool definition the client
sends. AF's stock `_prepare_tools_for_anthropic` doesn't emit it, so we
subclass and post-process. Tool input schemas must already be
strict-compatible (``additionalProperties: false``, all fields in
``required``); use ``OpenAIStrictModeBaseModel`` from
``openai_utils.pydantic_strict_mode`` as the input model base. The
schema constraint is the same one OpenAI structured outputs uses, hence
the shared base class.
"""

from collections.abc import Mapping
from typing import Any

from agent_framework import BaseChatClient, FunctionInvocationConfiguration
from agent_framework.anthropic import AnthropicClient
from agent_framework.openai import OpenAIChatCompletionClient


class _StrictAnthropicClient(AnthropicClient):
    """AnthropicClient that turns on Anthropic's strict tool-use mode.

    Stock AF emits tools as ``{"type": "custom", "name": ..., "input_schema": ...}``
    without ``strict``; we walk the prepared tool list and inject
    ``"strict": True`` on every custom entry. Server-side tools (web_search,
    code_execution, etc.) are not affected.
    """

    def _prepare_tools_for_anthropic(self, options: Mapping[str, Any]) -> dict[str, Any] | None:
        result = super()._prepare_tools_for_anthropic(options)
        if result is None:
            return None
        for tool in result.get("tools") or []:
            if isinstance(tool, dict) and tool.get("type") == "custom":
                tool["strict"] = True
        return result


def build_model_client(
    *,
    api: str,
    model: str,
    function_invocation_configuration: FunctionInvocationConfiguration | None = None,
    strict_tools: bool = False,
) -> BaseChatClient[Any]:
    kwargs: dict[str, Any] = {"model": model}
    if function_invocation_configuration is not None:
        kwargs["function_invocation_configuration"] = function_invocation_configuration
    if api == "openai":
        if strict_tools:
            raise ValueError("strict_tools=True is only wired for the Anthropic adapter so far.")
        return OpenAIChatCompletionClient(**kwargs)
    if api == "anthropic":
        cls = _StrictAnthropicClient if strict_tools else AnthropicClient
        return cls(**kwargs)
    raise ValueError(f"Unsupported API: {api!r}")
