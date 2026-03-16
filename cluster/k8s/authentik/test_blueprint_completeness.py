"""Test: all authentik blueprint YAML files listed in configMapGenerator."""

from __future__ import annotations

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_AUTHENTIK_KUSTOMIZATION = "_main/cluster/k8s/authentik/kustomization.yaml"


def test_authentik_blueprint_completeness() -> None:
    authentik_kust = get_required_path(_AUTHENTIK_KUSTOMIZATION)
    blueprints_dir = authentik_kust.parent / "blueprints"

    with authentik_kust.open() as f:
        doc = yaml.safe_load(f)

    listed_files: set[str] = set()
    for generator in doc.get("configMapGenerator", []):
        if generator.get("name") == "authentik-sso-blueprints":
            listed_files = {f.split("/")[-1] for f in generator.get("files", [])}
            break

    on_disk = {p.name for p in blueprints_dir.glob("*.yaml")}
    unlisted = sorted(on_disk - listed_files)

    assert not unlisted, (
        f"Add to authentik-sso-blueprints files list: {', '.join(f'blueprints/{name}' for name in unlisted)}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
