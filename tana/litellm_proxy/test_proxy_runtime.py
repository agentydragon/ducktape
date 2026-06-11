"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import asyncio
import importlib
import os
import subprocess
import textwrap
from pathlib import Path

import litellm
from litellm.proxy.proxy_server import ProxyConfig

from util.bazel.runfiles import get_required_path


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
        original_custom_provider_map = list(litellm.custom_provider_map)
        original_provider_list = list(litellm.provider_list)
        try:
            litellm.custom_provider_map = []
            litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]

            router, _, _ = await ProxyConfig().load_config(router=None, config_file_path=str(config_path))

            assert "tana" in litellm.provider_list
            assert router is not None
            deployments = router.get_model_list(model_name="gpt-4o-mini")
            assert deployments is not None
            assert len(deployments) == 1
            assert deployments[0]["litellm_params"]["model"] == "tana/gpt-4o-mini"
        finally:
            litellm.custom_provider_map = original_custom_provider_map
            litellm.provider_list = original_provider_list

    asyncio.run(load_config())
