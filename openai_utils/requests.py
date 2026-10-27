"""Typed request helpers for OpenAI endpoints."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, CompletionCreateParams
from openai.types.responses import Response

from openai_utils.errors import translate_context_length
from openai_utils.model import ResponsesResult


async def create_responses_result(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    """Create a Responses API result and translate context-length errors."""
    create: Callable[..., Awaitable[Response]] = cast(Callable[..., Awaitable[Response]], client.responses.create)
    sdk_response = await translate_context_length(create, **kwargs)
    return ResponsesResult.from_sdk(sdk_response)


async def create_chat_completion(client: AsyncOpenAI, params: CompletionCreateParams) -> ChatCompletion:
    """Create a non-streaming chat completion and translate context-length errors."""
    if params.get("stream"):
        raise ValueError("create_chat_completion does not support streaming (stream=True)")
    request_params = dict(params)
    request_params.pop("stream", None)
    create: Callable[..., Awaitable[ChatCompletion]] = cast(
        Callable[..., Awaitable[ChatCompletion]], client.chat.completions.create
    )
    return await translate_context_length(create, stream=False, **request_params)
