import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.bes import Output
from devinfra.ci.plan_releases import Release
from devinfra.ci.release_assets import nix_override_rows, release_paths, resolve_asset

BIN = "bazel-out/k8-fastbuild/bin"


def out(path: str, label: str = "//:a") -> Output:
    return Output(label=label, path=path, uri="u", digest="d", size=1, output_group="default")


def test_a_resolved_asset_is_the_streams_path_under_bb_out() -> None:
    """`bb remote build` materializes outputs on the runner under bb-out/."""
    by_label = {"//:a": [out(f"{BIN}/a.whl")]}
    assert resolve_asset(by_label, "//:a", "a.whl") == f"bb-out/{BIN}/a.whl"


def test_release_paths_follow_ssot_order() -> None:
    release = Release(
        pkg="p",
        targets="//:a //:b",
        filenames="a.whl b.zip",
        tests="",
        bazel_flags="",
        release_metadata="false",
        metadata_platform="",
    )
    by_label = {"//:a": [out(f"{BIN}/a.whl")], "//:b": [out(f"{BIN}/b.zip", label="//:b")]}
    assert release_paths(release, by_label) == [f"bb-out/{BIN}/a.whl", f"bb-out/{BIN}/b.zip"]


def test_an_unreported_target_fails_loud() -> None:
    """This runs after the build that should have produced the asset, so absence
    is a broken build or wrong target, never a row to skip."""
    with pytest.raises(SystemExit, match="not reported"):
        resolve_asset({}, "//:a", "a.whl")


def test_a_basename_contradicting_the_registry_fails_loud() -> None:
    """The filename is the release-download URL component sync-pins builds;
    publishing a file under another name would 404 every consumer of the pin."""
    by_label = {"//:a": [out(f"{BIN}/renamed.whl")]}
    with pytest.raises(SystemExit, match="filename"):
        resolve_asset(by_label, "//:a", "a.whl")


def test_nix_overrides_cover_exactly_the_gated_pins(tmp_path: Path) -> None:
    """Every pin of a nixPackage release gets an override row; binary drops the
    PR gate skips (bazel-ci already rebuilds them) get none."""
    spec = tmp_path / "artifact_targets.json"
    spec.write_text(
        json.dumps(
            {
                "pins": {
                    "gated": {"target": "//:g", "filename": "g.whl", "release": "gated"},
                    "binary": {"target": "//:x", "filename": "x", "release": "binary"},
                },
                "releases": {"gated": {"nixPackage": True}, "binary": {"nixPackage": False}},
            }
        )
    )
    by_label = {"//:g": [out(f"{BIN}/g.whl", label="//:g")]}
    assert nix_override_rows(spec, by_label) == [("gated", f"bb-out/{BIN}/g.whl")]


if __name__ == "__main__":
    pytest_bazel.main()
