"""The Codex catalog is read by the Codex the repository pins, with the windows the routes configure."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from cluster.cdk8s.agentplane.codex_models import codex_catalog
from cluster.cdk8s.model_selections import RUNNER_CONTEXT_OVERRIDES, STAGING_APP_MODELS, TESTING_APP_MODELS
from model_catalog.catalog import Provider, Route
from util.bazel.runfiles import get_required_path


def _codex_reads(catalog: str, directory: Path) -> dict[str, Any]:
    """Slug to entry, as the pinned Codex parses the catalog (it refuses one it cannot use)."""
    path = directory / "catalog.json"
    path.write_text(catalog)
    home = directory / "codex_home"
    home.mkdir()
    result = subprocess.run(
        [
            get_required_path(os.environ["CODEX_BINARY"]),
            "-c",
            f"model_catalog_json={json.dumps(str(path))}",
            "debug",
            "models",
        ],
        env={"CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return {model["slug"]: model for model in json.loads(result.stdout)["models"]}


@pytest.mark.parametrize("routes", [STAGING_APP_MODELS.codex, TESTING_APP_MODELS.codex], ids=["staging", "testing"])
def test_pinned_codex_reads_the_catalog(routes: tuple[Route, ...], tmp_path: Path) -> None:
    catalog = codex_catalog(routes)
    assert catalog is not None
    models = _codex_reads(catalog, tmp_path)

    assert {route.id for route in routes if route.upstream.provider is Provider.CHATGPT} <= models.keys()
    windows = {route.id: RUNNER_CONTEXT_OVERRIDES[route] for route in routes if route in RUNNER_CONTEXT_OVERRIDES}
    assert windows
    assert windows.items() <= {slug: model["context_window"] for slug, model in models.items()}.items()


if __name__ == "__main__":
    pytest_bazel.main()
