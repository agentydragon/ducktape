import json
import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles


@pytest.fixture
def runner(tmp_path):
    path = Runfiles.CreateOrRaise().Rlocation("ducktape/x/habitify_mcp/mcp_dev_runner")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PYTHON_DOTENV_DISABLED": "1",
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
        "HABITIFY_API_KEY": "ambient-synthetic-habitify-key",
        "HABITIFY_API_BASE_URL": "http://127.0.0.1:9",
    }
    return path, env


def test_inspector_config_lifecycle(tmp_path, runner):
    path, env = runner
    launcher = tmp_path / "npx"
    launcher.write_text(
        """#!/bin/sh
set -eu
test "$1" = "--yes"
test "$3" = "--config"
test -z "${HABITIFY_API_KEY+x}"
test -z "${HABITIFY_API_BASE_URL+x}"
test "$MCP_INSPECTOR_SECRET_STORE" = memory
test "$(stat -c %a "$4")" = 600
printf '%s\n' "$4" > config-path
cp "$4" config.json
exit 23
"""
    )
    launcher.chmod(0o755)

    explicit_key = "argument-synthetic-habitify-key"
    result = subprocess.run(
        [path, "--api-key", explicit_key, "--debug-tools"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    assert explicit_key not in result.stdout + result.stderr
    assert env["HABITIFY_API_KEY"] not in result.stdout + result.stderr
    assert not Path((tmp_path / "config-path").read_text().strip()).exists()

    entry = json.loads((tmp_path / "config.json").read_text())["mcpServers"]["Habitify"]
    assert Path(entry["command"]).is_file()
    assert entry["args"] == ["mcp", "--debug"]
    assert entry["env"]["HABITIFY_API_KEY"] == explicit_key
    assert entry["env"]["HABITIFY_API_BASE_URL"] == env["HABITIFY_API_BASE_URL"]


def test_missing_key(runner, tmp_path):
    path, env = runner
    del env["HABITIFY_API_KEY"]
    result = subprocess.run([path], env=env, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "HABITIFY_API_KEY is required" in result.stderr


if __name__ == "__main__":
    pytest_bazel.main()
