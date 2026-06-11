from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.parse import parse_qs

import httpx
import litellm
import pytest_bazel
from litellm.types.utils import Choices, GenericStreamingChunk, ModelResponse, Usage

from tana.litellm_proxy.custom_handler import tana_handler
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
                    {"role": "system", "content": "You are concise.", "cache_control": {"type": "ephemeral"}},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "call the tool later",
                                "cache_control": {"type": "ephemeral"},
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
                            "output": {"type": "text", "value": '{"ok":true}'},
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


def test_client_maps_claude_code_style_tool_request_to_llm_proxy_next() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxyNext"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"text": "ok"})

    async def run() -> TanaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), http_client=http)
            return await client.chat_completion(
                "claude-sonnet-4-20250514",
                [
                    {
                        "role": "system",
                        "content": [
                            {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}
                        ],
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"role": "user", "content": "Summarize this repository."},
                ],
                {
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "LSP__getDiagnostics",
                                "description": "Get language-server diagnostics.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                    "max_tokens": 32000,
                },
            )

    result = asyncio.run(run())

    assert result.text == "ok"
    body = seen_bodies[0]
    assert body["isStreaming"] is False
    assert body["args"]["userContext"] == "Ask Tana"
    assert body["args"]["options"]["maxOutputTokens"] == 32000
    assert body["dynamicTools"] == [
        {
            "name": "LSP__getDiagnostics",
            "description": "Get language-server diagnostics.",
            "kind": "mcpTool",
            "runtime": "client",
            "schema": {"type": "object", "properties": {}},
        }
    ]
    assert body["args"]["messages"][0] == {
        "role": "system",
        "content": "You are Claude Code.",
        "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
    }
    assert "cache_control" not in json.dumps(body)


def test_client_maps_anthropic_tool_transcript_to_tana_messages() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "id-token-1", "refresh_token": "refresh-2", "expires_in": "3600"}
            )
        assert request.url == "https://app.tana.inc/functions/llmProxyNext"
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"text": "ok"})

    async def run() -> TanaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(TanaProxyConfig(refresh_token="refresh-1"), http_client=http)
            return await client.chat_completion(
                "claude-sonnet-4-20250514",
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Let me inspect that."},
                            {"type": "tool_use", "id": "toolu_1", "name": "LSP", "input": {}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": "InputValidationError",
                                "is_error": True,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    },
                ],
                {"tools": [{"type": "function", "function": {"name": "LSP"}}]},
            )

    result = asyncio.run(run())

    assert result.text == "ok"
    assert seen_bodies[0]["args"]["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me inspect that."},
                {"type": "tool-call", "toolCallId": "toolu_1", "toolName": "LSP", "input": {}},
            ],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": "toolu_1",
                    "toolName": "LSP",
                    "output": {"type": "error-text", "value": "InputValidationError"},
                    "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                }
            ],
        },
    ]
    assert "tool_use" not in json.dumps(seen_bodies[0])
    assert "tool_result" not in json.dumps(seen_bodies[0])


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
                [
                    {
                        "role": "system",
                        "content": [
                            {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}
                        ],
                    },
                    {"role": "user", "content": "call the tool"},
                ],
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
    assert seen_bodies[0]["args"]["messages"][0] == {
        "role": "system",
        "content": "You are Claude Code.",
        "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
    }
    assert seen_bodies[0]["args"]["messages"][0]["providerOptions"] == {
        "anthropic": {"cacheControl": {"type": "ephemeral"}}
    }
    assert "cache_control" not in json.dumps(seen_bodies[0])
    assert seen_bodies[0]["dynamicTools"][0]["runtime"] == "client"


def test_reads_refresh_token_from_kubernetes_secret_json() -> None:
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert args == ["kubectl", "get", "secret", "-n", "tana-mcp", "tana-firebase-refresh-token", "-o", "json"]
        stdout = json.dumps({"data": {"refresh_token": base64.b64encode(b"refresh-token").decode("ascii")}})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    assert read_refresh_token_from_config(TanaProxyConfig(), runner=runner) == "refresh-token"


def test_client_rereads_external_refresh_token_source_after_id_token_expires() -> None:
    refresh_tokens_seen: list[str] = []
    reader_tokens = iter(["refresh-from-secret-1", "refresh-from-secret-2"])
    now = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            form = parse_qs(request.content.decode("utf-8"))
            refresh_token = form["refresh_token"][0]
            refresh_tokens_seen.append(refresh_token)
            token_number = len(refresh_tokens_seen)
            return httpx.Response(
                200,
                json={
                    "id_token": f"id-token-{token_number}",
                    "refresh_token": f"rotated-refresh-{token_number}",
                    "expires_in": "120",
                },
            )
        assert request.url == "https://app.tana.inc/functions/llmProxy"
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"text": "ok"})

    def refresh_token_reader(_cfg: TanaProxyConfig) -> str:
        return next(reader_tokens)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(
                TanaProxyConfig(), http_client=http, refresh_token_reader=refresh_token_reader, now=lambda: now[0]
            )
            await client.chat_completion("claude-test", [{"role": "user", "content": "first"}])
            now[0] = 1061.0
            await client.chat_completion("claude-test", [{"role": "user", "content": "second"}])

    asyncio.run(run())

    assert refresh_tokens_seen == ["refresh-from-secret-1", "refresh-from-secret-2"]


def test_client_does_not_adopt_rotated_refresh_token_from_firebase() -> None:
    refresh_tokens_seen: list[str] = []
    now = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            form = parse_qs(request.content.decode("utf-8"))
            refresh_token = form["refresh_token"][0]
            refresh_tokens_seen.append(refresh_token)
            token_number = len(refresh_tokens_seen)
            return httpx.Response(
                200,
                json={
                    "id_token": f"id-token-{token_number}",
                    "refresh_token": f"rotated-refresh-{token_number}",
                    "expires_in": "120",
                },
            )
        assert request.url == "https://app.tana.inc/functions/llmProxy"
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"text": "ok"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TanaProxyClient(
                TanaProxyConfig(refresh_token="configured-refresh-token"), http_client=http, now=lambda: now[0]
            )
            await client.chat_completion("claude-test", [{"role": "user", "content": "first"}])
            now[0] = 1061.0
            await client.chat_completion("claude-test", [{"role": "user", "content": "second"}])

    asyncio.run(run())

    assert refresh_tokens_seen == ["configured-refresh-token", "configured-refresh-token"]


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


def test_litellm_handler_astreaming_yields_chunks() -> None:
    class FakeClient(_NoStreamingClient):
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            raise AssertionError("async streaming test should not call non-streaming chat_completion")

        async def astream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> AsyncIterator[GenericStreamingChunk]:
            assert model == "claude-test"
            assert messages == [{"role": "user", "content": "hi"}]
            assert optional_params == {"stream": True}
            yield GenericStreamingChunk(text="async-pong", is_finished=False, finish_reason="", usage=None, index=0)
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

    async def collect_chunks() -> list[GenericStreamingChunk]:
        handler = TanaLiteLLM(FakeClient())
        stream = handler.astreaming(
            model="claude-test",
            messages=[{"role": "user", "content": "hi"}],
            api_base="",
            custom_prompt_dict={},
            model_response=cast(ModelResponse, None),
            print_verbose=lambda *args, **kwargs: None,
            encoding=None,
            api_key=None,
            logging_obj=None,
            optional_params={"stream": True},
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(collect_chunks())

    assert chunks[0]["text"] == "async-pong"
    assert chunks[-1]["is_finished"] is True
    assert chunks[-1]["finish_reason"] == "stop"


def test_litellm_routes_async_streaming_to_custom_provider() -> None:
    class FakeClient(_NoStreamingClient):
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            raise AssertionError("async streaming test should not call non-streaming chat_completion")

        async def astream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> AsyncIterator[GenericStreamingChunk]:
            assert model == "claude-test"
            assert messages == [{"role": "user", "content": "hi"}]
            assert optional_params == {"stream": True}
            yield GenericStreamingChunk(
                text="async-route-pong", is_finished=False, finish_reason="", usage=None, index=0
            )
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

    async def collect_chunks() -> list[Any]:
        register_litellm_provider(TanaLiteLLM(FakeClient()))
        stream = await litellm.acompletion(
            model="tana/claude-test", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(collect_chunks())

    assert chunks[0].choices[0].delta.content == "async-route-pong"
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_registers_tana_as_litellm_custom_provider() -> None:
    handler = register_litellm_provider(TanaLiteLLM())

    assert any(item["provider"] == "tana" and item["custom_handler"] is handler for item in litellm.custom_provider_map)
    assert "tana" in litellm.model_list_set


def test_registered_tana_provider_handles_async_litellm_completion() -> None:
    class FakeClient(_NoStreamingClient):
        async def chat_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> TanaChatResult:
            assert model == "claude-test"
            assert messages == [{"role": "user", "content": "hi"}]
            assert optional_params == {}
            return TanaChatResult(text="pong")

    original_custom_provider_map = list(litellm.custom_provider_map)
    original_provider_list = list(litellm.provider_list)
    original_custom_providers = list(litellm._custom_providers)
    original_model_list_set = set(litellm.model_list_set)
    try:
        litellm.custom_provider_map = []
        litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]
        litellm._custom_providers = [provider for provider in litellm._custom_providers if provider != "tana"]
        litellm.model_list_set.discard("tana")

        register_litellm_provider(TanaLiteLLM(FakeClient()))

        response = asyncio.run(
            litellm.acompletion(model="tana/claude-test", messages=[{"role": "user", "content": "hi"}])
        )

        assert response.choices[0].message.content == "pong"
    finally:
        litellm.custom_provider_map = original_custom_provider_map
        litellm.provider_list = original_provider_list
        litellm._custom_providers = original_custom_providers
        litellm.model_list_set = original_model_list_set


def test_custom_handler_module_exports_litellm_handler() -> None:
    assert isinstance(tana_handler, TanaLiteLLM)


if __name__ == "__main__":
    pytest_bazel.main()
