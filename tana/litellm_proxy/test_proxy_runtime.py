"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import litellm
import openai
import pytest
import pytest_bazel
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types import MessageParam, TextBlockParam, ToolParam, ToolUseBlockParam
from litellm.proxy import proxy_server
from litellm.proxy.proxy_server import ProxyConfig
from litellm.types.utils import GenericStreamingChunk
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from tana.litellm_proxy import custom_handler
from tana.litellm_proxy.provider import TanaChatResult, TanaLiteLLM, TanaProxyClient, TanaProxyConfig
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_in_loop


def test_litellm_proxy_binary_imports_server(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            model_list: []
            litellm_settings:
              drop_params: true
            """
        )
    )

    result = subprocess.run(
        [get_required_path("ducktape/tana/litellm_proxy/server_bin"), "--config", config_path, "--skip_server_startup"],
        capture_output=True,
        check=False,
        env=os.environ | {"LITELLM_MASTER_KEY": "sk-test"},
        text=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout


async def test_litellm_proxy_config_registers_custom_provider_before_router_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_litellm_provider: None
) -> None:
    handler_path = tmp_path / "tana/litellm_proxy/custom_handler.py"
    handler_path.parent.mkdir(parents=True)
    shutil.copy(get_required_path("ducktape/tana/litellm_proxy/custom_handler.py"), handler_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(get_required_path("_main/tana/litellm_proxy/testdata/custom_provider.yaml").read_text())

    class FakeClient:
        async def chat_completion(
            self,
            model: str,
            messages: Sequence[Mapping[str, Any]],
            optional_params: Mapping[str, Any] | None = None,
            *,
            refresh_token: str | None = None,
        ) -> TanaChatResult:
            assert model == "tana/gpt-4o-mini"
            assert messages == [{"role": "user", "content": "hi"}]
            assert refresh_token == "refresh-token"
            return TanaChatResult(text="pong")

        def stream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> Iterator[GenericStreamingChunk]:
            raise AssertionError("non-streaming test should not call stream_completion")

        async def astream_completion(
            self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
        ) -> AsyncIterator[GenericStreamingChunk]:
            raise AssertionError("non-streaming test should not call astream_completion")
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

    litellm.custom_provider_map = []
    litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]
    litellm._custom_providers = [provider for provider in litellm._custom_providers if provider != "tana"]
    litellm.model_list_set.discard("tana")

    router, _, _ = await ProxyConfig().load_config(router=None, config_file_path=str(config_path))

    assert router is not None
    monkeypatch.setattr(TanaLiteLLM, "_make_client", lambda _self, _config: FakeClient())
    response = await router.acompletion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

    assert response.choices[0].message.content == "pong"


async def test_litellm_router_forwards_local_reasoning_default_in_http_payload(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Use the pinned LiteLLM OpenAI adapter through a mock HTTP transport."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(get_required_path("_main/tana/litellm_proxy/testdata/local_reasoning.yaml").read_text())

    request_bodies: list[dict[str, Any]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3:4b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
    openai_client = openai.AsyncOpenAI(api_key="sk-test", base_url="http://ollama.test/v1", http_client=http_client)
    router, _, _ = await ProxyConfig().load_config(router=None, config_file_path=str(config_path))
    assert router is not None
    monkeypatch.setattr(router, "_get_async_openai_model_client", lambda *, deployment, kwargs: openai_client)

    try:
        response = await router.acompletion(model="rugged-local", messages=[{"role": "user", "content": "hi"}])
    finally:
        await openai_client.close()

    assert response.choices[0].message.content == "pong"
    assert request_bodies[0]["reasoning_effort"] == "none"


@asynccontextmanager
async def _serve_tana_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handle_tana_request: Callable[[httpx.Request], httpx.Response]
) -> AsyncIterator[str]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(get_required_path("_main/tana/litellm_proxy/testdata/custom_provider.yaml").read_text())
    monkeypatch.setenv("CONFIG_FILE_PATH", str(config_path))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200, json={"id_token": "test-id", "refresh_token": "test-refresh", "expires_in": "3600"}
            )
        assert request.url.host == "tana.invalid", f"unexpected external request: {request.url}"
        return handle_tana_request(request)

    old_map = list(litellm.custom_provider_map)
    old_providers = list(litellm.provider_list)
    old_custom = list(litellm._custom_providers)
    old_models = set(litellm.model_list_set)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as tana_http:
            monkeypatch.setattr(custom_handler, "tana_handler", TanaLiteLLM())
            monkeypatch.setattr(
                TanaLiteLLM,
                "_make_client",
                lambda _self, _config: TanaProxyClient(
                    TanaProxyConfig(firebase_api_key="test-key", functions_base_url="https://tana.invalid/functions"),
                    http_client=tana_http,
                ),
            )
            async with serve_app_in_loop(proxy_server.app) as origin:
                yield origin
    finally:
        litellm.custom_provider_map = old_map
        litellm.provider_list = old_providers
        litellm._custom_providers = old_custom
        litellm.model_list_set = old_models


class _KnownLiteLLMContractGapError(RuntimeError):
    """A pinned LiteLLM boundary reproduced by the HTTP contract probe."""


@pytest.mark.parametrize(
    ("include_usage", "require_cache_control"),
    [
        pytest.param(True, False, id="stream-tool-result-continuation"),
        pytest.param(
            True,
            True,
            id="system-cache-control",
            marks=pytest.mark.xfail(
                strict=True,
                raises=_KnownLiteLLMContractGapError,
                reason="Pinned LiteLLM 1.103.0 /v1/messages drops system cache_control before the custom provider",
            ),
        ),
        pytest.param(
            False,
            False,
            id="provider-response-without-usage",
            marks=pytest.mark.xfail(
                strict=True,
                raises=_KnownLiteLLMContractGapError,
                reason="Pinned LiteLLM 1.103.0 Anthropic response adapter requires ModelResponse.usage",
            ),
        ),
    ],
)
async def test_anthropic_http_stream_tool_result_continuation_uses_tana_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_usage: bool, require_cache_control: bool
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handle_tana_request(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        requests.append((request.url.path, request_body))
        if len(requests) == 1:
            assert request.url.path == "/functions/llmProxyNext"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"type":"tool-input-available","toolCallId":"toolu_test",'
                    b'"toolName":"lookup_fact","input":{}}\n'
                    b'data: {"type":"tool-input-available","toolCallId":"toolu_test",'
                    b'"toolName":"lookup_fact","input":{"topic":"Tana"}}\n'
                    b'data: {"type":"finish","messageMetadata":{"finishReason":"stop"}}\n'
                ),
                request=request,
            )

        assert request.url.path == "/functions/llmProxyNext"
        body: dict[str, Any] = {"text": "Tana is a knowledge workspace."}
        if include_usage:
            body["usage"] = {"inputTokens": 7, "outputTokens": 6}
        return httpx.Response(200, json=body, request=request)

    async with (
        _serve_tana_proxy(tmp_path, monkeypatch, handle_tana_request) as origin,
        AsyncAnthropic(api_key="sk-test", base_url=origin, max_retries=0) as client,
    ):
        system: list[TextBlockParam] = [
            {"type": "text", "text": "You are a coding assistant."},
            {"type": "text", "text": "Keep the workspace context intact.", "cache_control": {"type": "ephemeral"}},
        ]
        tools: list[ToolParam] = [
            {
                "name": "lookup_fact",
                "description": "Look up a fact.",
                "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
            }
        ]
        user_message: MessageParam = {"role": "user", "content": "Find a fact about Tana."}
        async with client.messages.stream(
            model="gpt-4o-mini", max_tokens=128, system=system, tools=tools, messages=[user_message]
        ) as stream:
            tool_message = await stream.get_final_message()

        tool_use = next(block for block in tool_message.content if block.type == "tool_use")
        assert tool_message.stop_reason == "tool_use"
        assert tool_use.name == "lookup_fact"
        assert tool_use.input == {"topic": "Tana"}

        tool_use_param: ToolUseBlockParam = {
            "type": "tool_use",
            "id": tool_use.id,
            "name": tool_use.name,
            "input": tool_use.input,
        }
        assistant_message: MessageParam = {"role": "assistant", "content": [tool_use_param]}
        tool_result_message: MessageParam = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use.id, "content": "Tana is a knowledge workspace."}
            ],
        }
        continuation_messages: list[MessageParam] = [user_message, assistant_message, tool_result_message]
        continuation_request = client.messages.create(
            model="gpt-4o-mini", max_tokens=128, system=system, tools=tools, messages=continuation_messages
        )
        if include_usage:
            continuation = await continuation_request
        else:
            with pytest.raises(APIStatusError) as error_info:
                await continuation_request
            error = error_info.value
            assert error.status_code == 500
            assert "ModelResponse" in error.response.text
            assert "usage" in error.response.text
            raise _KnownLiteLLMContractGapError(
                "LiteLLM 1.103.0 Anthropic adapter fails on a provider-supported response without usage"
            ) from error

    text_block = next(block for block in continuation.content if block.type == "text")
    assert text_block.text == "Tana is a knowledge workspace."
    assert [path for path, _body in requests] == ["/functions/llmProxyNext", "/functions/llmProxyNext"]
    streamed_body = requests[0][1]
    assert streamed_body["isStreaming"] is True
    system_messages = [message for message in streamed_body["args"]["messages"] if message["role"] == "system"]
    system_payload = json.dumps(system_messages)
    assert "Keep the workspace context intact." in system_payload
    assert streamed_body["dynamicTools"][0]["name"] == "lookup_fact"

    continuation_body = requests[1][1]
    assert continuation_body["isStreaming"] is False
    assert any(
        message["role"] == "assistant" and "tool-call" in json.dumps(message)
        for message in continuation_body["args"]["messages"]
    )
    assert any(
        message["role"] == "tool"
        and "tool-result" in json.dumps(message)
        and "Tana is a knowledge workspace." in json.dumps(message)
        for message in continuation_body["args"]["messages"]
    )
    if require_cache_control and "cacheControl" not in system_payload:
        raise _KnownLiteLLMContractGapError(
            "LiteLLM 1.103.0 /v1/messages drops system cache_control before the custom provider: "
            + json.dumps(system_messages, sort_keys=True)
        )


def _chat_messages() -> list[ChatCompletionMessageParam]:
    # LiteLLM accepts cache_control on content blocks, beyond the OpenAI SDK's types.
    system = cast(
        ChatCompletionMessageParam,
        {
            "role": "system",
            "content": [{"type": "text", "text": "Keep context.", "cache_control": {"type": "ephemeral"}}],
        },
    )
    return [system, {"role": "user", "content": "hi"}]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("include_usage", [False, True])
async def test_chat_completions_preserves_cache_and_optional_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool, include_usage: bool
) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        usage = {"inputTokens": 7, "outputTokens": 6} if include_usage else None
        if streaming:
            finish: dict[str, Any] = {"finishReason": "stop"}
            if usage is not None:
                finish["usage"] = usage
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"type":"text-delta","delta":"pong"}\n'
                    + "data: "
                    + json.dumps({"type": "finish", "messageMetadata": finish})
                    + "\n"
                ).encode(),
            )
        body: dict[str, Any] = {"text": "pong"}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    async with (
        _serve_tana_proxy(tmp_path, monkeypatch, handle) as origin,
        openai.AsyncOpenAI(api_key="sk-test", base_url=origin + "/v1", max_retries=0) as client,
    ):
        if streaming:
            chunks = [
                chunk
                async for chunk in await client.chat.completions.create(
                    model="gpt-4o-mini", messages=_chat_messages(), stream=True, stream_options={"include_usage": True}
                )
            ]
            assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "pong"
            usage_result = next((chunk.usage for chunk in chunks if chunk.usage is not None), None)
        else:
            result = await client.chat.completions.create(model="gpt-4o-mini", messages=_chat_messages())
            assert result.choices[0].message.content == "pong"
            usage_result = result.usage
            if not include_usage:
                assert usage_result is None
        if include_usage:
            assert usage_result is not None
            assert (usage_result.prompt_tokens, usage_result.completion_tokens) == (7, 6)

    assert len(requests) == 1
    assert requests[0]["args"]["messages"][0]["providerOptions"]["anthropic"]["cacheControl"] == {"type": "ephemeral"}


async def test_chat_completions_stream_tool_roundtrip_without_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/functions/llmProxyNext"
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"type":"tool-input-available","toolCallId":"call_test","toolName":"lookup_fact","input":{}}\n'
                    b'data: {"type":"tool-input-available","toolCallId":"call_test","toolName":"lookup_fact","input":{"topic":"Tana"}}\n'
                    b'data: {"type":"finish","messageMetadata":{"finishReason":"stop"}}\n'
                ),
            )
        return httpx.Response(200, json={"text": "Fact received."})

    async with (
        _serve_tana_proxy(tmp_path, monkeypatch, handle) as origin,
        openai.AsyncOpenAI(api_key="sk-test", base_url=origin + "/v1", max_retries=0) as client,
    ):
        tools: list[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": "lookup_fact",
                    "parameters": {
                        "type": "object",
                        "properties": {"topic": {"type": "string"}},
                        "required": ["topic"],
                    },
                },
            }
        ]
        messages = _chat_messages()
        chunks = [
            chunk
            async for chunk in await client.chat.completions.create(
                model="gpt-4o-mini", messages=messages, stream=True, tools=tools
            )
        ]
        calls = [call for chunk in chunks if chunk.choices for call in chunk.choices[0].delta.tool_calls or []]
        first = calls[0]
        assert first.id == "call_test"
        assert first.function is not None
        assert first.function.name == "lookup_fact"
        arguments = "".join(call.function.arguments or "" for call in calls if call.function is not None)
        assert json.loads(arguments) == {"topic": "Tana"}
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": first.id,
                            "type": "function",
                            "function": {"name": first.function.name, "arguments": arguments},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": first.id, "content": "A fact about Tana."},
            ]
        )
        result = await client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=tools)
        assert result.choices[0].message.content == "Fact received."
        assert result.usage is None

    assert len(requests) == 2
    assert requests[0]["dynamicTools"][0]["name"] == "lookup_fact"
    continuation = requests[1]["args"]["messages"]
    assert continuation[-2]["content"][0]["type"] == "tool-call"
    assert continuation[-1]["role"] == "tool"
    assert continuation[-1]["content"][0]["toolCallId"] == "call_test"
    assert continuation[-1]["content"][0]["output"]["value"] == "A fact about Tana."
    assert continuation[0]["providerOptions"]["anthropic"]["cacheControl"] == {"type": "ephemeral"}


if __name__ == "__main__":
    pytest_bazel.main()
