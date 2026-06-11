"""Replay chat client for deterministic testing.

A minimal Microsoft Agent Framework chat client that returns pre-scripted
`ChatResponse` objects in order. Composed of the same layer stack as
production AF clients (`FunctionInvocationLayer` + `ChatMiddlewareLayer` +
`ChatTelemetryLayer` over `BaseChatClient`), so `Agent.run()` exercises
the real tool-dispatch loop against scripted responses.

Consumers that hand-roll their own tool-dispatch loop (and don't want the
function-invocation layer to auto-dispatch tool calls between scripted
responses) should pass `function_invocation_configuration={"enabled": False}`
to disable the layer's loop.
"""

from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, ClassVar

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    FunctionInvocationConfiguration,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)
from agent_framework.observability import ChatTelemetryLayer


class ReplayChatClient(
    FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], ChatTelemetryLayer[Any], BaseChatClient[Any]
):
    """Chat client that replays scripted responses in order."""

    OTEL_PROVIDER_NAME: ClassVar[str] = "replay"

    def __init__(
        self,
        responses: Sequence[ChatResponse],
        *,
        function_invocation_configuration: FunctionInvocationConfiguration | None = None,
    ) -> None:
        super().__init__(middleware=[], function_invocation_configuration=function_invocation_configuration)
        self._responses = list(responses)
        self._index = 0

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        if stream:
            raise NotImplementedError("ReplayChatClient does not support streaming")

        async def _get() -> ChatResponse:
            if self._index >= len(self._responses):
                raise RuntimeError(f"ReplayChatClient exhausted: used all {len(self._responses)} responses")
            response = self._responses[self._index]
            self._index += 1
            return response

        return _get()
