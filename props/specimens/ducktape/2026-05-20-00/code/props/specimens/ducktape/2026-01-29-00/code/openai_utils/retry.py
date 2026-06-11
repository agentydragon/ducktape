from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

import httpx
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, CompletionCreateParams
from openai.types.responses import Response
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from openai_utils.errors import translate_context_length
from openai_utils.model import OpenAIModelProto, ResponsesRequest, ResponsesResult

if TYPE_CHECKING:
    pass

# Default retry policy: 5 attempts, exponential backoff with jitter (~0.5s..60s)
_DEFAULT_ATTEMPTS = 10
_DEFAULT_INITIAL = 0.5
_DEFAULT_MAX = 60.0
_RETRY_ON: Iterable[type[BaseException]] = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
    httpx.TimeoutException,
    httpx.ConnectError,
)


P = ParamSpec("P")
T = TypeVar("T")


def retry_decorator(
    attempts: int = _DEFAULT_ATTEMPTS,
    initial: float = _DEFAULT_INITIAL,
    maximum: float = _DEFAULT_MAX,
    retry_exceptions: Iterable[type[BaseException]] = _RETRY_ON,
):
    """Return a tenacity.retry decorator with our standard settings."""

    tenacity_decorator = retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=initial, max=maximum),
        retry=retry_if_exception_type(tuple(retry_exceptions)),
    )

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        wrapped = tenacity_decorator(func)

        @functools.wraps(func)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T:
            call = cast(Callable[P, Awaitable[T]], wrapped)
            return await call(*args, **kwargs)

        return inner

    return decorator


@retry_decorator()
async def responses_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    create: Callable[..., Awaitable[Response]] = cast(Callable[..., Awaitable[Response]], client.responses.create)
    sdk_resp = await translate_context_length(create, **kwargs)
    return ResponsesResult.from_sdk(sdk_resp)


@retry_decorator()
async def chat_create_with_retries(client: AsyncOpenAI, params: CompletionCreateParams) -> ChatCompletion:
    """Create a chat completion with retries (non-streaming only).

    Args:
        client: AsyncOpenAI client
        params: Chat completion parameters (must have stream=False or omit stream)

    Returns:
        ChatCompletion response

    Raises:
        ValueError: If stream=True is in params
    """
    if params.get("stream"):
        raise ValueError("chat_create_with_retries does not support streaming (stream=True)")
    # Explicit stream=False ensures we get ChatCompletion (non-streaming) overload
    create: Callable[..., Awaitable[ChatCompletion]] = cast(
        Callable[..., Awaitable[ChatCompletion]], client.chat.completions.create
    )
    return await translate_context_length(create, stream=False, **params)


@dataclass
class RetryingOpenAIModel(OpenAIModelProto):
    """Retry-decorated wrapper around an OpenAIModel-like base implementing our protocol."""

    base: OpenAIModelProto
    model: str = ""

    def __post_init__(self) -> None:
        self.model = self.base.model

    @retry_decorator()
    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        result = await self.base.responses_create(req)
        return cast(ResponsesResult, result)
