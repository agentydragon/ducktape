"""Cross-language consumers receive generated projections of canonical selections."""

import json

import pytest_bazel

from devinfra.bazel.runfiles import get_required_path
from model_catalog.nix import OUTPUT_PATH, claude_wrapper_models


def test_wrapper_projection_matches_committed_json() -> None:
    assert claude_wrapper_models() == json.loads(get_required_path(f"ducktape/{OUTPUT_PATH}").read_text())


if __name__ == "__main__":
    pytest_bazel.main()
