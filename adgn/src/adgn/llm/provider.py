"""Provider protocol and base interfaces.

Defines the LLMProvider protocol that all provider implementations must satisfy.
"""

from __future__ import annotations

from typing import Protocol

from .types import CompletionRequest, CompletionResult


class LLMProvider(Protocol):
    """Provider-agnostic LLM interface.

    All LLM providers (OpenAI, Anthropic, etc.) implement this protocol by translating
    between the provider-agnostic types (CompletionRequest/CompletionResult) and their
    native API formats.

    Each provider handles:
    - Translation to/from native API types
    - Provider-specific retry logic and error handling
    - Provider-specific features and limitations
    """

    @property
    def model(self) -> str:
        """Return the model identifier."""
        ...

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        """Generate a completion.

        Translates the provider-agnostic request to the provider's native API format,
        calls the API, and translates the response back to the provider-agnostic format.
        """
        ...


class LLMModel(Protocol):
    """Legacy alias for backward compatibility.

    Prefer using LLMProvider for new code.
    """

    @property
    def model(self) -> str: ...

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...
