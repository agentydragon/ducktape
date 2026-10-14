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
import pytest_bazel
from litellm.proxy.proxy_server import ProxyConfig
from litellm.types.utils import GenericStreamingChunk

from tana.litellm_proxy.provider import TanaChatResult, TanaLiteLLM
from util.bazel.runfiles import get_required_path


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
    config_path.write_text(
        textwrap.dedent(
            """
            model_list:
              - model_name: gpt-4o-mini
                litellm_params:
                  model: tana/tana/gpt-4o-mini
                  custom_llm_provider: tana
                  api_key: refresh-token
                model_info:
                  mode: chat
                  supports_function_calling: true
            litellm_settings:
              drop_params: true
              custom_provider_map:
                - provider: tana
                  custom_handler: tana.litellm_proxy.custom_handler.tana_handler
            """
        )
    )

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
    config_path.write_text(
        textwrap.dedent(
            """
            model_list:
              - model_name: rugged-local
                litellm_params:
                  model: openai/qwen3:4b
                  api_base: http://ollama.test/v1
                  api_key: sk-test
                  extra_body:
                    reasoning_effort: none
            litellm_settings:
              drop_params: true
            router_settings:
              num_retries: 0
            """
        )
    )

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


if __name__ == "__main__":
    pytest_bazel.main()
