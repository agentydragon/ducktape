"""Ties ssh-mcp's known_hosts entries to the canonical host-key sources they must agree with, so a
stale or hand-edited line fails here instead of as a silent host-key-verification failure at
connect time."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def _known_hosts(path: Path) -> dict[str, str]:
    """Hostname -> "<key-type> <base64>", for plain (non-bracketed) entries."""
    entries: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        host, _, key = line.partition(" ")
        entries[host] = key
    return entries


def test_public_coder_devbox_matches_its_pinned_host_key() -> None:
    """The one target with a canonical committed host-key file: known_hosts must carry exactly
    that key, under exactly the hostname ssh-mcp's settings.yaml names as the target."""
    known_hosts = _known_hosts(get_required_path("_main/cluster/k8s/ssh-mcp/known_hosts"))
    settings = yaml.safe_load(get_required_path("_main/cluster/k8s/ssh-mcp/settings.yaml").read_text())
    devbox_hosts = {target["host"] for target in settings["targets"] if "public-coder-devbox" in target["host"]}
    assert devbox_hosts, "no ssh-mcp target names public-coder-devbox; this test's premise is stale"

    pinned = get_required_path("_main/ssh_keys/public-coder-devbox-host.pub").read_text().split()
    pinned_key = f"{pinned[0]} {pinned[1]}"

    for host in devbox_hosts:
        assert host in known_hosts, f"ssh-mcp target host {host!r} has no known_hosts entry"
        assert known_hosts[host] == pinned_key, (
            f"known_hosts for {host!r} does not match ssh_keys/public-coder-devbox-host.pub"
        )


def test_public_coder_devbox_matches_sshpiper_pin() -> None:
    """sshpiper (a second, independent consumer) pins the same host key for the same hostname;
    both plain and bracketed [host]:22 spellings. If either drifts, one of the two consumers is
    silently trusting a different identity than the other."""
    known_hosts = _known_hosts(get_required_path("_main/cluster/k8s/ssh-mcp/known_hosts"))
    pipe = yaml.safe_load(
        get_required_path("_main/cluster/k8s/agents/public-coder-agent/sshpiper/pipe-devbox.yaml").read_text()
    )
    known_hosts_data = base64.b64decode(pipe["spec"]["to"]["known_hosts_data"]).decode()
    piper_lines = {line.partition(" ")[0]: line.partition(" ")[2] for line in known_hosts_data.splitlines() if line}

    devbox_host = one(host for host in known_hosts if "public-coder-devbox" in host)
    assert piper_lines[devbox_host] == known_hosts[devbox_host]
    assert piper_lines[f"[{devbox_host}]:22"] == known_hosts[devbox_host]


if __name__ == "__main__":
    pytest_bazel.main()
