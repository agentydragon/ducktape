"""The published runner image carries both harnesses, the tools they reach for, and the runner as
its entrypoint, which the SandboxTemplate leaves in place and only passes arguments to."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest_bazel

from util.bazel.runfiles import get_required_path


def _blob(layout: Path, descriptor: dict[str, Any]) -> Path:
    algorithm, digest = cast(str, descriptor["digest"]).split(":", 1)
    return layout / "blobs" / algorithm / digest


def _layer_members(layout: Path) -> tuple[dict[str, tarfile.TarInfo], dict[str, Any]]:
    index = json.loads((layout / "index.json").read_text())
    manifest = json.loads(_blob(layout, index["manifests"][0]).read_text())
    config = json.loads(_blob(layout, manifest["config"]).read_text())
    members: dict[str, tarfile.TarInfo] = {}
    for descriptor in manifest["layers"]:
        with tarfile.open(_blob(layout, descriptor), mode="r:*") as layer:
            for member in layer.getmembers():
                members[member.name.removeprefix("./")] = member
    return members, config


def test_image_contains_both_harnesses_and_the_runner_entry_point() -> None:
    # Through the .rloc indirection, which carries the image tree and its blobs into runfiles.
    layout = get_required_path(get_required_path("_main/x/agentplane/runner/image_rloc.rloc").read_text().strip())
    members, config = _layer_members(layout)
    expected_executables = {
        "usr/local/bin/claude",
        "opt/codex/bin/codex",
        "opt/codex/codex-path/rg",
        "usr/bin/git",
        "usr/bin/rg",
        "usr/bin/curl",
    }
    missing = expected_executables - members.keys()
    assert not missing, f"{missing=}"
    assert all(members[path].mode & 0o111 for path in expected_executables)
    # The native Codex executable resolves its resources relative to itself.
    assert "opt/codex/codex-package.json" in members
    assert "etc/ssl/certs/ca-certificates.crt" in members
    # The launcher finds its runfiles next to its own real path, so the entrypoint names it
    # directly; a symlink in front of it would start a runner that exits at once.
    assert config["config"]["Entrypoint"] == ["/x/agentplane/runner/runner_image_bin"]
    assert members["x/agentplane/runner/runner_image_bin"].mode & 0o111
    assert members["home/runner"].isdir()
    assert members["home/runner"].uid == 1000
    environment = dict(entry.split("=", 1) for entry in config["config"]["Env"])
    assert environment["HOME"] == "/home/runner"
    assert environment["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert config["config"]["User"] == "1000:1000"


if __name__ == "__main__":
    pytest_bazel.main()
