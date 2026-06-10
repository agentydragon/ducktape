"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import importlib


def test_litellm_proxy_server_imports() -> None:
    importlib.import_module("litellm.proxy.proxy_cli")
    importlib.import_module("litellm.proxy.proxy_server")
