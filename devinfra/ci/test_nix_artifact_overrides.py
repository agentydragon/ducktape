import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.buildbuddy import cli
from devinfra.buildbuddy.ci.bes import Invocation, Output
from devinfra.ci import nix_artifact_overrides

BIN = "bazel-out/k8-fastbuild/bin"


def output(label: str, path: str, uri: str, *, group: str = "default", aspect: str = "") -> Output:
    return Output(label=label, path=path, uri=uri, digest="digest", size=1, output_group=group, aspect=aspect)


def write_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "pins": {
                    "first": {"target": "//:first", "filename": "first.whl", "release": "gate"},
                    "second": {"target": "//:second", "filename": "second.zip", "release": "gate"},
                    "ungated": {"target": "//:ignored", "filename": "ignored.whl", "release": "binary"},
                },
                "releases": {"gate": {"nixPackage": True}, "binary": {"nixPackage": False}},
            }
        )
    )


def test_fetches_each_invocation_once_in_one_batch_and_emits_absolute_pin_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = tmp_path / "artifact_targets.json"
    write_registry(registry)
    first_path = f"{BIN}/first.whl"
    second_path = f"{BIN}/second.zip"
    invocations = {
        "one": Invocation(
            outputs=[
                output("//:first", first_path, "bytestream://fake/first"),
                output("//:first", f"{BIN}/lint.txt", "bytestream://fake/lint", group="mypy"),
            ],
            test_status={},
        ),
        "two": Invocation(
            outputs=[
                output("//:second", second_path, "bytestream://fake/second"),
                output("//:ignored", f"{BIN}/ignored.whl", "bytestream://fake/ignored"),
                output("//:first", f"{BIN}/aspect.whl", "bytestream://fake/aspect", aspect="lint%mypy"),
            ],
            test_status={},
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "artifact_targets.json"
    write_registry(registry)
    monkeypatch.setattr(
        nix_artifact_overrides,
        "read",
        lambda _invocation_id: Invocation(
            outputs=[output("//:first", f"{BIN}/renamed.whl", "bytestream://fake/renamed")], test_status={}
        ),
    )

    with pytest.raises(SystemExit, match="filename"):
        nix_artifact_overrides.fetch_overrides(["one"], registry)


def test_surfaces_a_failed_bulk_download_without_emitting_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "artifact_targets.json"
    write_registry(registry)
    monkeypatch.setattr(
        nix_artifact_overrides,
        "read",
        lambda _invocation_id: Invocation(
            outputs=[
                output("//:first", f"{BIN}/first.whl", "bytestream://fake/first"),
                output("//:second", f"{BIN}/second.zip", "bytestream://fake/second"),
            ],
            test_status={},
        ),
    )

    def fail_download(*_args: str, **_kwargs) -> bytes:
        raise cli.CLIError("HTTP 503: unavailable")

    monkeypatch.setattr(cli, "run", fail_download)

    with pytest.raises(SystemExit, match=r"failed to download 2 Nix artifact.*HTTP 503"):
        nix_artifact_overrides.fetch_overrides(["one"], registry)


def test_rejects_a_download_that_does_not_materialize_the_resolved_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = tmp_path / "artifact_targets.json"
    write_registry(registry)
    monkeypatch.setattr(
        nix_artifact_overrides,
        "read",
        lambda _invocation_id: Invocation(
            outputs=[
                output("//:first", f"{BIN}/first.whl", "bytestream://fake/first"),
                output("//:second", f"{BIN}/second.zip", "bytestream://fake/second"),
            ],
            test_status={},
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: b"")

    with pytest.raises(SystemExit, match=r"completed without creating bb-out/.*/first.whl"):
        nix_artifact_overrides.fetch_overrides(["one"], registry)


if __name__ == "__main__":
    pytest_bazel.main()
