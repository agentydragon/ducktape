"""Runtime smoke tests for the LiteLLM proxy entrypoint closure."""

import importlib
import os
import subprocess
import textwrap
from pathlib import Path

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
