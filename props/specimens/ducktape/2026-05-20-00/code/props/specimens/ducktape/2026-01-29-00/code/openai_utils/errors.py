"""OpenAI adapter exceptions and translation helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeVar

from openai import BadRequestError


@dataclass(slots=True)
class ContextLengthExceededError(Exception):
    """Raised when OpenAI rejects a request for exceeding the context window."""

    message: str
    request_id: str | None = None
    code: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = self.message
        if self.request_id:
            base = f"{base} (request_id={self.request_id})"
        if self.code:
            base = f"{base} [code={self.code}]"
        return base


P = ParamSpec("P")
T = TypeVar("T")


async def translate_context_length(call: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
    """Execute an OpenAI SDK call and translate context-length errors."""

    try:
        return await call(*args, **kwargs)
    except BadRequestError as exc:
        if exc.code == "context_length_exceeded":
            raise ContextLengthExceededError(message=str(exc), request_id=exc.request_id, code=exc.code) from exc
        raise
