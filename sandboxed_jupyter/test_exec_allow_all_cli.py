import shutil
import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from sandboxed_jupyter.sandboxer import build_sandboxer_command


def test_sandboxer_cli_allow_all_runs_echo(tmp_path: Path, require_sandbox_exec):
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    policy = tmp_path / "policy_allow_all.yaml"
    policy_dict = {
        "env": {
            "set": {"TMPDIR": (tmp_path / "tmp").as_posix(), "HOME": tmp_path.as_posix(), "PYTHONUNBUFFERED": "1"},
            "passthrough": [],
        },
        "fs": {"read_paths": ["/"], "write_paths": [tmp_path.as_posix()]},
        "net": {"mode": "open"},
        "platform": {"trace": False, "seatbelt": {"extra_allow": {"sysctl_read": True, "file_read_extra": []}}},
    }
    policy.write_text(yaml.safe_dump(policy_dict, sort_keys=False))

    cmd = build_sandboxer_command(
        policy, ["/bin/sh", "-c", "echo SANDBOXER_OK"], python=shutil.which("python3") or "python3"
    )
    cp = subprocess.run(cmd, text=True, capture_output=True, check=False)

    if cp.returncode != 0:
        print("STDOUT:\n" + cp.stdout)
        print("STDERR:\n" + cp.stderr)

    assert cp.returncode == 0
    assert "SANDBOXER_OK" in cp.stdout


if __name__ == "__main__":
    pytest_bazel.main()
