import json
from pathlib import Path

import pytest

from devinfra.buildbuddy import cli
from devinfra.buildbuddy.ci.bes import Invocation, Output
from devinfra.ci import nix_artifact_overrides, release_assets

BIN = "bazel-out/k8-fastbuild/bin"


def output(label: str, path: str, uri: str, *, group: str = "default", aspect: str = "") -> Output:
    return Output(label=label, path=path, uri=uri, digest="digest", output_group=group, aspect=aspect)


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "artifact_targets.json"
    path.write_text(
        json.dumps(
            {
                "pins": {
                    "first": {"target": "//:first", "filename": "first.whl", "release": "gate"},
                    "second": {"target": "//:second", "filename": "second.zip", "release": "gate"},
                    "binary": {"target": "//:ignored", "filename": "ignored.whl", "release": "binary"},
                    "ungated": {"target": "//:ungated", "filename": "ungated.whl", "release": "ungated"},
                },
                "releases": {"gate": {"nixPackage": True}, "binary": {"nixPackage": False}, "ungated": {}},
            }
        )
    )
    return path


def test_nix_gate_builds_and_fetches_each_selected_pin(
    tmp_path: Path, registry: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    release_assets.main(["nix-wheel-inputs", "--artifact-targets", str(registry)])
    assert capsys.readouterr().out.splitlines() == [
        "bazel_targets=//:first //:second",
        "nix_checks=.#checks.x86_64-linux.artifact-gate",
    ]
    monkeypatch.chdir(tmp_path)
    first_path = f"{BIN}/first.whl"
    second_path = f"{BIN}/second.zip"
    invocations = {
        "one": Invocation(
            outputs=[
                output("//:first", first_path, "bytestream://fake/first"),
                output("//:first", f"{BIN}/lint.txt", "bytestream://fake/lint", group="mypy"),
            ]
        ),
        "two": Invocation(
            outputs=[
                output("//:second", second_path, "bytestream://fake/second"),
                output("//:ignored", f"{BIN}/ignored.whl", "bytestream://fake/ignored"),
                output("//:first", f"{BIN}/aspect.whl", "bytestream://fake/aspect", aspect="lint%mypy"),
            ]
        ),
    }
    exports = []
    calls = []

    def fake_read(invocation_id: str) -> Invocation:
        exports.append(invocation_id)
        return invocations[invocation_id]

    def fake_run(*args: str, **_kwargs) -> bytes:
        calls.append(args)
        manifest_path = Path(args[-1])
        entries = json.loads(manifest_path.read_text())
        assert entries == [
            {"uri": "bytestream://fake/first", "destination": f"bb-out/{first_path}", "invocation_id": "one"},
            {"uri": "bytestream://fake/second", "destination": f"bb-out/{second_path}", "invocation_id": "two"},
        ]
        for entry in entries:
            destination = Path(entry["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"asset")
        return b""

    monkeypatch.setattr(nix_artifact_overrides, "read", fake_read)
    monkeypatch.setattr(cli, "run", fake_run)

    nix_artifact_overrides.main(["--invocations", "one,two", "--artifact-targets", str(registry)])

    assert exports == ["one", "two"]
    assert len(calls) == 1
    assert calls[0][:3] == ("blob", "download", "--manifest")
    assert json.loads(capsys.readouterr().out) == {
        "first": str((tmp_path / f"bb-out/{first_path}").resolve()),
        "second": str((tmp_path / f"bb-out/{second_path}").resolve()),
    }


def test_rejects_registry_filename_that_differs_from_the_default_output(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        nix_artifact_overrides,
        "read",
        lambda _invocation_id: Invocation(
            outputs=[output("//:first", f"{BIN}/renamed.whl", "bytestream://fake/renamed")]
        ),
    )

    with pytest.raises(SystemExit, match="filename"):
        nix_artifact_overrides.main(["--invocations", "one", "--artifact-targets", str(registry)])


@pytest.fixture
def invocation_with_gated_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    build = Invocation(
        outputs=[
            output("//:first", f"{BIN}/first.whl", "bytestream://fake/first"),
            output("//:second", f"{BIN}/second.zip", "bytestream://fake/second"),
        ]
    )
    monkeypatch.setattr(nix_artifact_overrides, "read", lambda _invocation_id: build)


@pytest.mark.usefixtures("invocation_with_gated_assets")
def test_surfaces_a_failed_bulk_download_without_emitting_overrides(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_download(*_args: str, **_kwargs) -> bytes:
        raise cli.CLIError("HTTP 503: unavailable")

    monkeypatch.setattr(cli, "run", fail_download)

    with pytest.raises(SystemExit, match=r"failed to download 2 Nix artifact.*HTTP 503"):
        nix_artifact_overrides.fetch_overrides(["one"], registry)


@pytest.mark.usefixtures("invocation_with_gated_assets")
def test_rejects_a_download_that_does_not_materialize_the_resolved_asset(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: b"")

    with pytest.raises(SystemExit, match=r"completed without creating bb-out/.*/first.whl"):
        nix_artifact_overrides.fetch_overrides(["one"], registry)
