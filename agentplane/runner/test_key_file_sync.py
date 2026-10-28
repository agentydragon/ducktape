"""Atomic rotation and failure retention at the Secret-to-file boundary."""

import base64
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from agentplane.runner.key_file_sync import _refresh_all


def test_rotation_replaces_path_without_changing_open_reader(tmp_path: Path) -> None:
    destination = tmp_path / "keys" / "api-key"
    config = tmp_path / "keys.json"
    config.write_text(
        json.dumps(
            [
                {
                    "namespace": "test-namespace",
                    "secret_name": "test-key",
                    "secret_key": "api-key",
                    "mount_path": str(destination.parent),
                    "file_name": destination.name,
                }
            ]
        )
    )

    def response(value: bytes) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"data": {"api-key": base64.b64encode(value).decode()}}), ""
        )

    with patch("agentplane.runner.key_file_sync.subprocess.run", side_effect=[response(b"before"), response(b"after")]):
        assert _refresh_all(config, group_id=os.getgid())
        with destination.open("rb") as reader:
            assert _refresh_all(config, group_id=os.getgid())
            assert reader.read() == b"before"
        assert destination.read_bytes() == b"after"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o440
    assert destination.stat().st_gid == os.getgid()
    assert sorted(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "private-stderr-do-not-log"),
        subprocess.CompletedProcess([], 0, '{"data":{"api-key":"private-invalid-base64"}}', ""),
        subprocess.CompletedProcess([], 0, '{"data":{}}', ""),
    ],
)
def test_failed_refresh_retains_previous_file_and_redacts_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, result: subprocess.CompletedProcess[str]
) -> None:
    destination = tmp_path / "api-key"
    destination.write_bytes(b"previous-complete-key")
    config = tmp_path / "keys.json"
    config.write_text(
        json.dumps(
            [
                {
                    "namespace": "test-namespace",
                    "secret_name": "test-key",
                    "secret_key": "api-key",
                    "mount_path": str(tmp_path),
                    "file_name": destination.name,
                }
            ]
        )
    )
    with patch("agentplane.runner.key_file_sync.subprocess.run", return_value=result):
        assert not _refresh_all(config, group_id=None)
    assert destination.read_bytes() == b"previous-complete-key"
    assert "Could not refresh runner key files" in caplog.text
    assert "private" not in caplog.text
    assert "previous-complete-key" not in caplog.text
    assert sorted(tmp_path.iterdir()) == sorted([config, destination])


if __name__ == "__main__":
    pytest_bazel.main()
