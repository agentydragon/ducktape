from dataclasses import replace

import pytest

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


@pytest.mark.parametrize(
    ("outputs", "error"),
    [
        ([], "not reported"),
        ([replace(out(f"{BIN}/lint.txt"), aspect="mypy")], "not reported"),
        ([out(f"{BIN}/a.whl"), out(f"{BIN}/extra.txt")], "default outputs"),
        ([out(f"{BIN}/renamed.whl")], "filename"),
    ],
    ids=["missing", "aspect-only", "ambiguous", "wrong-name"],
)
def test_asset_resolution_fails_loudly_before_publication(outputs: list[Output], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        resolve_asset({"//:a": outputs}, "//:a", "a.whl")
