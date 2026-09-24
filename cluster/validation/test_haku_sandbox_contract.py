"""Contracts for Haku sandbox deployment wiring."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest_bazel
import yaml
from more_itertools import one


def _object(path: Path, kind: str, name: str) -> dict[str, Any]:
    return one(
        obj for obj in yaml.safe_load_all(path.read_text()) if obj["kind"] == kind and obj["metadata"]["name"] == name
    )


def _haku_template(k8s_dir: Path) -> dict[str, Any]:
    return _object(k8s_dir / "haku/workspaces/app/haku-workspaces.k8s.yaml", "SandboxTemplate", "haku")


def sandbox_env(template: dict[str, object]) -> dict[str, dict[str, Any]]:
    container = cast(dict[str, Any], template["spec"]["podTemplate"]["spec"]["containers"][0])  # type: ignore[index]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_haku_sandbox_satisfies_the_shared_bootstrap(k8s_dir: Path) -> None:
    """`haku-sandbox-setup.sh` writes ~/.netrc from `${HAKU_GIT_USERNAME:?}` / `${HAKU_GIT_PASSWORD:?}`
    and aborts the whole claim when either is unset. That is the right behaviour and exactly why
    it wants a test: the failure lands at claim time, silently otherwise.
    """
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "the bootstrap declares no required variables — did the ${VAR:?} form change?"

    assert required <= set(sandbox_env(_haku_template(k8s_dir)))


if __name__ == "__main__":
    pytest_bazel.main()
