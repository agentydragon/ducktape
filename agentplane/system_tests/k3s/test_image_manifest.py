"""The grouped manifest carries each image's runfiles into its consumer."""

import json
import os

import pytest_bazel
from python.runfiles import Runfiles


def test_grouped_manifest_and_layout_runfiles(runfiles: Runfiles) -> None:
    root = runfiles.root()
    groups = json.loads((root / os.environ["IMAGE_MANIFEST_RLOCATION"]).read_text())
    assert set(groups) == {"fixture", "pki", "vm", "guest"}
    assert set(groups["fixture"]) == {"example/fixture-a:1", "example/fixture-b:1"}
    assert groups["pki"] == {"example/fixture-a:1": groups["fixture"]["example/fixture-a:1"]}
    assert groups["vm"] == groups["guest"] == {}
    assert (root / groups["fixture"]["example/fixture-a:1"]).read_text() == "layout a\n"
    assert (root / groups["fixture"]["example/fixture-b:1"]).read_text() == "layout b\n"


if __name__ == "__main__":
    pytest_bazel.main()
