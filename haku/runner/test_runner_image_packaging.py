"""The one published Haku harness image contains both native CLIs and runner tools."""

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


def _image(layout: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    index = json.loads((layout / "index.json").read_text())
    manifest = json.loads(_blob(layout, index["manifests"][0]).read_text())
    config = json.loads(_blob(layout, manifest["config"]).read_text())
    return manifest, config


def _layer_members(layout: Path, manifest: dict[str, Any]) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for descriptor in manifest["layers"]:
        with tarfile.open(_blob(layout, descriptor), mode="r:*") as layer:
            for member in layer.getmembers():
                members[member.name.removeprefix("./")] = member
    return members


def test_runner_image_contains_both_harnesses_and_required_tools() -> None:
    layout = get_required_path("_main/haku/runner/runner_image")
    manifest, config = _image(layout)
    members = _layer_members(layout, manifest)

    expected_executables = {
        "usr/local/bin/claude",
        "opt/codex/bin/codex",
        "opt/codex/bin/codex-code-mode-host",
        "opt/codex/codex-path/rg",
        "opt/codex/codex-resources/bwrap",
        "opt/codex/codex-resources/zsh/bin/zsh",
        "usr/local/bin/kubectl",
        "usr/bin/git",
    }
    assert expected_executables <= members.keys()
    assert "etc/ssl/certs/ca-certificates.crt" in members

    # The native Codex executable resolves these resources relative to itself, so flattening the
    # archive would make an apparently complete image fail only when the harness starts.
    assert "opt/codex/codex-package.json" in members
    assert all(members[path].mode & 0o111 for path in expected_executables)

    environment = dict(entry.split("=", 1) for entry in config["config"]["Env"])
    assert environment["HAKU_CLAUDE_PATH"] == "/usr/local/bin/claude"
    assert environment["HAKU_CODEX_PATH"] == "/opt/codex/bin/codex"
    assert environment["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"


if __name__ == "__main__":
    pytest_bazel.main()
