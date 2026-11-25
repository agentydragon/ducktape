"""Defaults for unexpected calls to the Tana transport in LiteLLM tests."""

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any, Never

from litellm.types.utils import GenericStreamingChunk

from tana.litellm_proxy.provider import TanaChatResult


def _unexpected_call(method: str) -> Never:
    raise AssertionError(f"unexpected {method}")


class StubTanaClient:
    """Override the transport method the scenario expects to use."""

    async def chat_completion(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any] | None = None,
        *,
        refresh_token: str,
    ) -> TanaChatResult:
        _unexpected_call("chat_completion")

    def stream_completion(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any] | None = None,
        *,
        refresh_token: str,
    ) -> Iterator[GenericStreamingChunk]:
        _unexpected_call("stream_completion")

    async def astream_completion(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any] | None = None,
        *,
        refresh_token: str,
    ) -> AsyncIterator[GenericStreamingChunk]:
        yield _unexpected_call("astream_completion")
