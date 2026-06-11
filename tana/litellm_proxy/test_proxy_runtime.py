"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import asyncio
import importlib
import os
import subprocess
import textwrap
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import litellm
from litellm.proxy.proxy_server import ProxyConfig
from litellm.types.utils import GenericStreamingChunk

from tana.litellm_proxy.provider import TanaChatResult
from util.bazel.runfiles import get_required_path


def _get_tana_handler() -> Any:
    return importlib.import_module("tana.litellm_proxy.custom_handler").tana_handler


def test_litellm_proxy_server_imports() -> None:
    importlib.import_module("litellm.proxy.proxy_cli")
    importlib.import_module("litellm.proxy.proxy_server")


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


def test_litellm_proxy_config_registers_custom_provider_before_router_build(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            model_list:
              - model_name: gpt-4o-mini
                litellm_params:
                  model: tana/gpt-4o-mini
                  custom_llm_provider: tana
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

    async def load_config() -> None:
        class FakeClient:
            async def chat_completion(
                self,
                model: str,
                messages: Sequence[Mapping[str, Any]],
                optional_params: Mapping[str, Any] | None = None,
            ) -> TanaChatResult:
                assert model == "gpt-4o-mini"
                assert messages == [{"role": "user", "content": "hi"}]
                return TanaChatResult(text="pong")

            def stream_completion(
                self,
                model: str,
                messages: Sequence[Mapping[str, Any]],
                optional_params: Mapping[str, Any] | None = None,
            ) -> Iterator[GenericStreamingChunk]:
                raise AssertionError("non-streaming test should not call stream_completion")

            async def astream_completion(
                self,
                model: str,
                messages: Sequence[Mapping[str, Any]],
                optional_params: Mapping[str, Any] | None = None,
            ) -> AsyncIterator[GenericStreamingChunk]:
                raise AssertionError("non-streaming test should not call astream_completion")
                yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=None, index=0)

        original_custom_provider_map = list(litellm.custom_provider_map)
        original_provider_list = list(litellm.provider_list)
        original_custom_providers = list(litellm._custom_providers)
        original_model_list_set = set(litellm.model_list_set)
        original_client = None
        try:
            litellm.custom_provider_map = []
            litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]
            litellm._custom_providers = [provider for provider in litellm._custom_providers if provider != "tana"]
            litellm.model_list_set.discard("tana")

            router, _, _ = await ProxyConfig().load_config(router=None, config_file_path=str(config_path))

            assert "tana" in litellm.provider_list
            assert "tana" in litellm.model_list_set
            assert litellm.get_llm_provider(model="tana/gpt-4o-mini")[1] == "tana"
            assert router is not None
            deployments = router.get_model_list(model_name="gpt-4o-mini")
            assert deployments is not None
            assert len(deployments) == 1
            assert deployments[0]["litellm_params"]["model"] == "tana/gpt-4o-mini"
            assert deployments[0]["litellm_params"]["custom_llm_provider"] == "tana"

            tana_handler = _get_tana_handler()
            original_client = tana_handler._client
            tana_handler._client = FakeClient()
            response = await router.acompletion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

            assert response.choices[0].message.content == "pong"
        finally:
            if original_client is not None:
                tana_handler = _get_tana_handler()
                tana_handler._client = original_client
            litellm.custom_provider_map = original_custom_provider_map
            litellm.provider_list = original_provider_list
            litellm._custom_providers = original_custom_providers
            litellm.model_list_set = original_model_list_set

    asyncio.run(load_config())
