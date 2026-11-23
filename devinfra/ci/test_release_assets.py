from dataclasses import replace

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Output
from devinfra.ci.release_assets import Release, release_paths, resolve_asset

BIN = "bazel-out/k8-fastbuild/bin"


def out(path: str, label: str = "//:a") -> Output:
    return Output(label=label, path=path, uri="u", digest="d", output_group="default")


def test_a_resolved_asset_is_the_streams_path_under_bb_out() -> None:
    """`bb remote build` materializes outputs on the runner under bb-out/."""
    asset = out(f"{BIN}/a.whl")
    by_label = {"//:a": [replace(asset, output_group="_validation"), replace(asset, aspect="mypy"), asset, asset]}
    assert resolve_asset(by_label, "//:a", "a.whl") == f"bb-out/{BIN}/a.whl"


def test_release_paths_follow_ssot_order() -> None:
    release = Release(pkg="p", assets=(("//:a", "a.whl"), ("//:b", "b.zip")))
    by_label = {"//:a": [out(f"{BIN}/a.whl")], "//:b": [out(f"{BIN}/b.zip", label="//:b")]}
    assert release_paths(release, by_label) == [f"bb-out/{BIN}/a.whl", f"bb-out/{BIN}/b.zip"]


def test_an_unreported_target_fails_loud() -> None:
    """This runs after the build that should have produced the asset, so absence
    is a broken build or wrong target, never a row to skip."""
    with pytest.raises(ValueError, match="not reported"):
        resolve_asset({}, "//:a", "a.whl")


def test_a_basename_contradicting_the_registry_fails_loud() -> None:
    """The filename is the release-download URL component sync-pins builds;
    publishing a file under another name would 404 every consumer of the pin."""
    by_label = {"//:a": [out(f"{BIN}/renamed.whl")]}
    with pytest.raises(ValueError, match="filename"):
        resolve_asset(by_label, "//:a", "a.whl")


if __name__ == "__main__":
    pytest_bazel.main()
