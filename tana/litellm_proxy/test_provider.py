from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any, Protocol, cast

import httpx
import litellm
import pytest_bazel
from litellm.types.utils import Choices, GenericStreamingChunk, Usage

from tana.litellm_proxy.provider import (
    TanaChatResult,
    TanaLiteLLM,
    TanaProxyClient,
    TanaProxyConfig,
    read_refresh_token_from_config,
    register_litellm_provider,
)


class _ModelResponseWithUsage(Protocol):
    usage: Usage


class _NoStreamingClient:
    def stream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> Iterator[GenericStreamingChunk]:
        raise AssertionError("non-streaming test should not call stream_completion")

    async def astream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[GenericStreamingChunk]:
        raise AssertionError("non-streaming test should not call astream_completion")
        yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)


def test_client_maps_basic_chat_request() -> None:
    seen_requests: list[httpx.Request] = []
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxy"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        assert request.headers["authorization"] == "Bearer id-token-1"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"text": "hello from tana", "usage": {"inputTokens": 2, "outputTokens": 3}},
        )

    async def run() -> TanaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), http_client=http)
            return await client.chat_completion(
                "tana/claude-test",
                [{"role": "user", "content": "hi"}],
                {
                    "temperature": 0.25,
                    "top_p": 0.9,
                    "max_tokens": 12,
                    "stop": "END",
                    "frequency_penalty": 0.1,
                    "presence_penalty": 0.2,
                },
            )

    result = asyncio.run(run())

    assert result.text == "hello from tana"
    assert result.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert len(seen_requests) == 2
    body = seen_bodies[0]
    assert body == {
        "isStreaming": False,
        "args": {
            "userContext": "Generic AI Query",
            "messages": [{"role": "user", "content": "hi"}],
            "options": {
                "model": "claude-test",
                "ignoreLargeContextWarning": True,
                "ignoreOutOfCreditsWarning": False,
                "temperature": 0.25,
                "topP": 0.9,
                "frequencyPenalty": 0.1,
                "presencePenalty": 0.2,
                "maxOutputTokens": 12,
                "stopStrings": ["END"],
            },
        },
    }


def test_client_maps_message_envelopes_without_prompt_collapsing() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxy"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"text": "ok"})

    async def run() -> TanaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), http_client=http)
            return await client.chat_completion(
                "claude-test",
                [
                    {
                        "role": "system",
                        "content": "You are concise.",
                        "provider_options": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "call the tool later",
                                "provider_options": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                            },
                            {"type": "reasoning", "text": "prior scratchpad"},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup_demo_fact", "arguments": '{"topic":"tana-litellm-tool"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "name": "lookup_demo_fact", "content": '{"ok":true}'},
                ],
                {"provider_options": {"openai": {"promptCacheKey": "conversation-1"}}},
            )

    result = asyncio.run(run())

    assert result.text == "ok"
    body = seen_bodies[0]
    assert "prompt" not in body["args"]
    assert body == {
        "isStreaming": False,
        "args": {
            "userContext": "Generic AI Query",
            "messages": [
                {
                    "role": "system",
                    "content": "You are concise.",
                    "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "call the tool later",
                            "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                        },
                        {"type": "reasoning", "text": "prior scratchpad"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool-call",
                            "toolCallId": "call-1",
                            "toolName": "lookup_demo_fact",
                            "input": {"topic": "tana-litellm-tool"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "call-1",
                            "toolName": "lookup_demo_fact",
                            "output": '{"ok":true}',
                        }
                    ],
                },
            ],
            "options": {
                "model": "claude-test",
                "ignoreLargeContextWarning": True,
                "ignoreOutOfCreditsWarning": False,
                "providerOptions": {"openai": {"promptCacheKey": "conversation-1"}},
            },
        },
    }


def test_client_maps_tool_request_to_llm_proxy_next() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxyNext"
        assert request.headers["authorization"] == "Bearer id-token-1"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "text": "",
                "toolCalls": [
                    {"toolCallId": "call-1", "toolName": "lookup_demo_fact", "input": {"topic": "tana-litellm-tool"}}
                ],
                "usage": {"promptTokens": 4, "completionTokens": 5},
            },
        )

    async def run() -> TanaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), http_client=http)
            return await client.chat_completion(
                "claude-test",
                [{"role": "user", "content": "call the tool"}],
                {
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup_demo_fact",
                                "description": "Look up a demo fact.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"topic": {"type": "string"}},
                                    "required": ["topic"],
                                },
                            },
                        }
                    ],
                    "max_tokens": 32,
                },
            )

    result = asyncio.run(run())

    assert result.text == ""
    assert result.tool_calls == [
        {"toolCallId": "call-1", "toolName": "lookup_demo_fact", "input": {"topic": "tana-litellm-tool"}}
    ]
    assert seen_bodies == [
        {
            "isStreaming": False,
            "args": {
                "userContext": "Ask Tana",
                "messages": [{"role": "user", "content": "call the tool"}],
                "options": {
                    "model": "claude-test",
                    "ignoreLargeContextWarning": True,
                    "ignoreOutOfCreditsWarning": False,
                    "maxOutputTokens": 32,
                },
            },
            "dynamicTools": [
                {
                    "name": "lookup_demo_fact",
                    "description": "Look up a demo fact.",
                    "kind": "mcpTool",
                    "runtime": "client",
                    "schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
                }
            ],
        }
    ]


def test_client_streams_text_chunks_from_llm_proxy() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxy"
        assert request.headers["authorization"] == "Bearer id-token-1"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"type":"text-delta","delta":"hello"}\n'
                b'0:" world"\n'
                b'data: {"type":"finish","finishReason":"stop",'
                b'"messageMetadata":{"usage":{"promptTokens":2,"completionTokens":3}}}\n'
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), sync_http_client=http)
        chunks = list(
            client.stream_completion(
                "tana/claude-test", [{"role": "user", "content": "hi"}], {"temperature": 0.0, "max_tokens": 16}
            )
        )

    assert [chunk["text"] for chunk in chunks if chunk["text"]] == ["hello", " world"]
    assert chunks[-1]["is_finished"] is True
    assert chunks[-1]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert seen_bodies == [
        {
            "isStreaming": True,
            "args": {
                "userContext": "Generic AI Query",
                "messages": [{"role": "user", "content": "hi"}],
                "options": {
                    "model": "claude-test",
                    "ignoreLargeContextWarning": True,
                    "ignoreOutOfCreditsWarning": False,
                    "temperature": 0.0,
                    "maxOutputTokens": 16,
                },
            },
        }
    ]


def test_client_streams_tool_calls_from_llm_proxy_next() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxyNext"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"type":"tool-input-available","toolCallId":"call-1",'
                b'"toolName":"lookup_demo_fact","input":"{}"}\n'
                b'data: {"type":"tool-input-available","toolCallId":"call-1",'
                b'"toolName":"lookup_demo_fact","input":{"topic":"tana-litellm-tool"}}\n'
                b'data: {"type":"finish","messageMetadata":{"finishReason":"stop",'
                b'"usage":{"inputTokens":4,"outputTokens":5}}}\n'
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), sync_http_client=http)
        chunks = list(
            client.stream_completion(
                "claude-test",
                [{"role": "user", "content": "call the tool"}],
                {"tools": [{"type": "function", "function": {"name": "lookup_demo_fact"}}]},
            )
        )

    tool_chunks = [chunk for chunk in chunks if chunk.get("tool_use") is not None]
    assert len(tool_chunks) == 2
    initial_tool_use = tool_chunks[0]["tool_use"]
    assert initial_tool_use is not None
    assert initial_tool_use["function"]["arguments"] == ""
    tool_use = tool_chunks[1]["tool_use"]
    assert tool_use is not None
    assert tool_use["id"] == "call-1"
    assert tool_use["type"] == "function"
    assert tool_use["function"]["name"] == "lookup_demo_fact"
    assert json.loads(tool_use["function"]["arguments"]) == {"topic": "tana-litellm-tool"}
    assert chunks[-1]["usage"] == {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
    assert seen_bodies[0]["isStreaming"] is True
    assert seen_bodies[0]["args"]["userContext"] == "Ask Tana"
    assert seen_bodies[0]["dynamicTools"][0]["runtime"] == "client"


def test_reads_refresh_token_from_kubernetes_secret_json() -> None:
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert args == ["kubectl", "get", "secret", "-n", "tana-mcp", "tana-firebase-refresh-token", "-o", "json"]
        stdout = json.dumps({"data": {"refresh_token": base64.b64encode(b"refresh-token").decode("ascii")}})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    assert read_refresh_token_from_config(TanaProxyConfig(), runner=runner) == "refresh-token"


def test_litellm_handler_returns_model_response() -> None:
    class FakeClient(_NoStreamingClient):
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            assert model == "claude-test"
            assert messages == [{"role": "user", "content": "hi"}]
            assert optional_params == {"temperature": 0.0}
            return TanaChatResult(text="pong", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    handler = TanaLiteLLM(FakeClient())
    response = asyncio.run(
        handler.acompletion(
            model="claude-test", messages=[{"role": "user", "content": "hi"}], optional_params={"temperature": 0.0}
        )
    )

    choice = response.choices[0]
    assert isinstance(choice, Choices)
    assert response.model == "tana/claude-test"
    assert choice.message.content == "pong"

    response_with_usage = cast(_ModelResponseWithUsage, response)
    assert isinstance(response_with_usage.usage, Usage)
    assert response_with_usage.usage.prompt_tokens == 1


def test_litellm_handler_returns_tool_calls() -> None:
    class FakeClient(_NoStreamingClient):
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            assert model == "claude-test"
            assert optional_params is not None
            assert "tools" in optional_params
            return TanaChatResult(
                text="",
                tool_calls=[
                    {"toolCallId": "call-1", "toolName": "lookup_demo_fact", "input": {"topic": "tana-litellm-tool"}}
                ],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    handler = TanaLiteLLM(FakeClient())
    response = asyncio.run(
        handler.acompletion(
            model="claude-test",
            messages=[{"role": "user", "content": "call the tool"}],
            optional_params={"tools": [{"type": "function", "function": {"name": "lookup_demo_fact"}}]},
        )
    )

    choice = response.choices[0]
    assert isinstance(choice, Choices)
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    tool_call = choice.message.tool_calls[0]
    assert tool_call.id == "call-1"
    assert tool_call.type == "function"
    assert tool_call.function.name == "lookup_demo_fact"
    assert json.loads(tool_call.function.arguments) == {"topic": "tana-litellm-tool"}


def test_litellm_routes_streaming_to_custom_provider() -> None:
    class FakeClient:
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            raise AssertionError("streaming test should not call non-streaming chat_completion")

        def stream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> Iterator[GenericStreamingChunk]:
            assert model == "claude-test"
            assert messages == [{"role": "user", "content": "hi"}]
            assert optional_params == {"stream": True}
            yield GenericStreamingChunk(text="pong", is_finished=False, finish_reason="", usage=None, index=0)
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

        async def astream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> AsyncIterator[GenericStreamingChunk]:
            raise AssertionError("sync streaming test should not call astream_completion")
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

    register_litellm_provider(TanaLiteLLM(FakeClient()))
    stream = litellm.completion(model="tana/claude-test", messages=[{"role": "user", "content": "hi"}], stream=True)

    chunks = list(stream)

    assert chunks[0].choices[0].delta.content == "pong"
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_registers_tana_as_litellm_custom_provider() -> None:
    handler = register_litellm_provider(TanaLiteLLM())

    assert any(item["provider"] == "tana" and item["custom_handler"] is handler for item in litellm.custom_provider_map)


if __name__ == "__main__":
    pytest_bazel.main()
