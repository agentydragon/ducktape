"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

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
    tmp_path: Path, monkeypatch: Any
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

    original_custom_provider_map = list(litellm.custom_provider_map)
    original_provider_list = list(litellm.provider_list)
    original_custom_providers = list(litellm._custom_providers)
    original_model_list_set = set(litellm.model_list_set)
    try:
        litellm.custom_provider_map = []
        litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]
        litellm._custom_providers = [provider for provider in litellm._custom_providers if provider != "tana"]
        litellm.model_list_set.discard("tana")

        router, _, _ = await ProxyConfig().load_config(router=None, config_file_path=str(config_path))

        assert router is not None
        monkeypatch.setattr(TanaLiteLLM, "_make_client", lambda _self, _config: FakeClient())
        response = await router.acompletion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

        assert response.choices[0].message.content == "pong"
    finally:
        litellm.custom_provider_map = original_custom_provider_map
        litellm.provider_list = original_provider_list
        litellm._custom_providers = original_custom_providers
        litellm.model_list_set = original_model_list_set


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
    config_path = tmp_path / "config.yaml"
    config_path.write_text(get_required_path("_main/tana/litellm_proxy/testdata/custom_provider.yaml").read_text())

    requests: list[tuple[str, dict[str, Any]]] = []

    def handle_tana_request(request: httpx.Request) -> httpx.Response:
        if request.url.host == "securetoken.googleapis.com":
            return httpx.Response(
                200,
                json={"id_token": "test-id-token", "refresh_token": "test-refresh-token", "expires_in": "3600"},
                request=request,
            )

        assert request.url.host == "tana.invalid", f"unexpected external request: {request.url}"
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

    monkeypatch.setenv("CONFIG_FILE_PATH", str(config_path))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    old_custom_provider_map = list(litellm.custom_provider_map)
    old_provider_list = list(litellm.provider_list)
    old_custom_providers = list(litellm._custom_providers)
    old_model_list_set = set(litellm.model_list_set)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_tana_request)) as tana_http:
            monkeypatch.setattr(custom_handler, "tana_handler", TanaLiteLLM())
            monkeypatch.setattr(
                TanaLiteLLM,
                "_make_client",
                lambda _self, _config: TanaProxyClient(
                    TanaProxyConfig(
                        firebase_api_key="test-firebase-key", functions_base_url="https://tana.invalid/functions"
                    ),
                    http_client=tana_http,
                ),
            )

            async with (
                serve_app_in_loop(proxy_server.app) as origin,
                AsyncAnthropic(api_key="sk-test", base_url=origin, max_retries=0) as client,
            ):
                system: list[TextBlockParam] = [
                    {"type": "text", "text": "You are a coding assistant."},
                    {
                        "type": "text",
                        "text": "Keep the workspace context intact.",
                        "cache_control": {"type": "ephemeral"},
                    },
                ]
                tools: list[ToolParam] = [
                    {
                        "name": "lookup_fact",
                        "description": "Look up a fact.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}},
                            "required": ["topic"],
                        },
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
    finally:
        litellm.custom_provider_map = old_custom_provider_map
        litellm.provider_list = old_provider_list
        litellm._custom_providers = old_custom_providers
        litellm.model_list_set = old_model_list_set


if __name__ == "__main__":
    pytest_bazel.main()
