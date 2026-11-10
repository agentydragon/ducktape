"""Cross-language consumers receive generated projections of canonical selections."""

import json

import pytest_bazel
from python.runfiles import Runfiles

from model_catalog.nix import OUTPUT_PATH, claude_wrapper_models


def test_wrapper_projection_matches_committed_json() -> None:
    assert claude_wrapper_models() == json.loads(
        (Runfiles.CreateOrRaise().root() / f"ducktape/{OUTPUT_PATH}").read_text()
    )


if __name__ == "__main__":
    pytest_bazel.main()
