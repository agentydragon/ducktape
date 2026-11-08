"""Exercise the shipped launcher entrypoint and required host-side tools."""

from __future__ import annotations

import subprocess

import pytest_bazel

from agentplane.firecracker import launcher_test_image
from devinfra.oci.oci import load_oci_image


def _run(image: str, *args: str, entrypoint: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm"]
    if entrypoint:
        command.extend(["--entrypoint", entrypoint])
    result = subprocess.run([*command, image, *args], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result


def test_shipped_launcher_starts_and_has_required_tools() -> None:
    image = load_oci_image(launcher_test_image.LAYOUT_RLOCATION)

    help_result = _run(image, "--help")
    assert "-config" in help_result.stdout + help_result.stderr

    version = _run(image, "--version", entrypoint="/usr/local/bin/firecracker")
    assert "Firecracker v1.17.0" in version.stdout

    tools = "cp truncate mkfs.ext4 xorriso blkid ip iptables ip6tables"
    _run(image, "-c", f'for tool in {tools}; do command -v "$tool" || exit 1; done', entrypoint="/bin/sh")


if __name__ == "__main__":
    pytest_bazel.main()
