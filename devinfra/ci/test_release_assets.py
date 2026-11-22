import json
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Output
from devinfra.ci.release_assets import Release, main, nix_override_assets, release_paths, resolve_asset

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


def test_nix_gate_builds_and_overrides_each_pin_of_a_checked_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "artifact_targets.json"
    spec.write_text(
        json.dumps(
            {
                "pins": {
                    "wheel": {"target": "//:wheel", "filename": "app.whl", "release": "bundle"},
                    "binary": {"target": "//:binary", "filename": "binary", "release": "binary"},
                    "extension": {"target": "//:extension", "filename": "app.zip", "release": "bundle"},
                    "ungated": {"target": "//:ungated", "filename": "ungated", "release": "ungated"},
                },
                "releases": {"bundle": {"nixPackage": True}, "binary": {"nixPackage": False}, "ungated": {}},
            }
        )
    )
    wheel = out(f"{BIN}/app.whl", label="//:wheel")
    extension = out(f"{BIN}/app.zip", label="//:extension")
    main(["nix-wheel-inputs", "--artifact-targets", str(spec)])
    assert capsys.readouterr().out.splitlines() == [
        "bazel_targets=//:wheel //:extension",
        "nix_checks=.#checks.x86_64-linux.artifact-bundle",
    ]
    assert nix_override_assets(spec, {wheel.label: [wheel], extension.label: [extension]}) == [
        ("wheel", wheel),
        ("extension", extension),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
