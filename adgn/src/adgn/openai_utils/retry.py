from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
import functools
from typing import Any, ParamSpec, TypeVar, cast

import httpx
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .model import ResponsesResult, convert_sdk_response

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
    sdk_resp = await client.responses.create(**kwargs)
    return convert_sdk_response(sdk_resp)


@retry_decorator()
async def chat_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ChatCompletion:
    # kwargs should contain: messages=..., model=..., etc.
    return await client.chat.completions.create(**kwargs)
