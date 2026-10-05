import hashlib
import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci import nix_runner_image

BASE_IMAGE = "ghcr.io/agentydragon/rbe"
RUNNER_IMAGE = "ghcr.io/agentydragon/buildbuddy-remote-runner"


def write_pins(
    root: Path,
    *,
    base_image: str = BASE_IMAGE,
    base_digest: str = "sha256:base",
    runner_image: str = RUNNER_IMAGE,
    runner_digest: str = "sha256:old-runner",
) -> None:
    pins = root / "devinfra" / "image_pins.json"
    pins.parent.mkdir(parents=True, exist_ok=True)
    pins.write_text(
        json.dumps(
            {
                "rbe_container": {"image": base_image, "digest": base_digest},
                "buildbuddy_remote_runner": {"image": runner_image, "digest": runner_digest},
            }
        )
    )


@pytest.mark.parametrize(
    ("pinned_image", "pinned_digest", "expected"),
    [
        (RUNNER_IMAGE, "sha256:built", False),
        (RUNNER_IMAGE, "sha256:old-runner", True),
        ("ghcr.io/agentydragon/other-runner", "sha256:built", True),
    ],
)
def test_publish_decision_uses_both_repository_and_digest(
    tmp_path: Path, pinned_image: str, pinned_digest: str, expected: bool
) -> None:
    write_pins(tmp_path, runner_image=pinned_image, runner_digest=pinned_digest)

    assert nix_runner_image.needs_publish(tmp_path, {"digest": "sha256:built"}, RUNNER_IMAGE) is expected


def test_verify_rejects_a_changed_base_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_pins(tmp_path, base_digest="sha256:new-base")
    monkeypatch.setattr(nix_runner_image, "derivation", lambda _base_path: "drv-old")
    state = {"base_ref": f"{BASE_IMAGE}@sha256:old-base", "base_path": "/nix/store/base", "derivation": "drv-old"}

    with pytest.raises(ValueError, match="RBE base pin changed"):
        nix_runner_image.verify(tmp_path, state)


def test_verify_rejects_a_changed_runner_derivation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_pins(tmp_path)
    monkeypatch.setattr(nix_runner_image, "derivation", lambda _base_path: "drv-new")
    state = {"base_ref": f"{BASE_IMAGE}@sha256:base", "base_path": "/nix/store/base", "derivation": "drv-old"}

    with pytest.raises(ValueError, match="runner image derivation changed"):
        nix_runner_image.verify(tmp_path, state)


def test_runtime_config_check_rejects_a_dropped_base_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"User": "1000:1000", "Entrypoint": ["/bin/runner"]},
    }
    candidate_config = {"os": "linux", "architecture": "amd64", "config": {"Entrypoint": ["/bin/runner"]}}
    configs = iter([json.dumps(base_config), json.dumps(candidate_config)])
    monkeypatch.setattr(nix_runner_image, "command", lambda *_args, **_kwargs: next(configs))

    with pytest.raises(ValueError, match="changed the base runtime config"):
        nix_runner_image.verify_runtime_config("/nix/store/base.tar", tmp_path / "oci")


def test_manifest_digest_hashes_the_raw_manifest_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = b'{"schemaVersion":2}\n'
    monkeypatch.setattr(nix_runner_image.subprocess, "check_output", lambda *_args: manifest)

    assert nix_runner_image.manifest_digest(tmp_path) == "sha256:" + hashlib.sha256(manifest).hexdigest()


def test_publish_preserves_digests_and_rejects_a_registry_manifest_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assembled_manifest = b'{"schemaVersion":2}\n'
    published_manifest = b'{"schemaVersion":2}'
    commands: list[tuple[str, ...]] = []

    def record_command(*args: str) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(nix_runner_image, "command", record_command)
    monkeypatch.setattr(nix_runner_image.subprocess, "check_output", lambda *_args: published_manifest)
    state = {"layout": str(tmp_path / "oci"), "digest": "sha256:" + hashlib.sha256(assembled_manifest).hexdigest()}

    with pytest.raises(ValueError, match="published manifest differs"):
        nix_runner_image.publish(state, RUNNER_IMAGE, "test-tag")

    assert len(commands) == 1
    assert "--preserve-digests" in commands[0]


def test_reproducibility_check_rejects_a_different_rebuilt_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_pins(tmp_path)
    manifests = iter([b"first assembly", b"different rebuild"])
    runtime_config = json.dumps(
        {"os": "linux", "architecture": "amd64", "config": {"User": "1000:1000", "Entrypoint": ["/bin/runner"]}}
    )

    def fake_command(*args: str, env: dict[str, str] | None = None) -> str:
        del env
        if args[:3] == ("skopeo", "inspect", "--config"):
            return runtime_config
        if args[0] == "skopeo" and args[-1].startswith("docker-archive:"):
            archive = args[-1].removeprefix("docker-archive:").split(":runner-base:", 1)[0]
            Path(archive).write_bytes(b"base image archive")
        elif args[:3] == ("nix", "store", "add-file"):
            return "/nix/store/base.tar"
        elif args[:3] == ("nix", "eval", "--impure"):
            return "/nix/store/runner.drv"
        elif args[:2] == ("nix", "build"):
            return "/nix/store/runner.tar"
        return ""

    monkeypatch.setattr(nix_runner_image, "command", fake_command)
    monkeypatch.setattr(nix_runner_image.subprocess, "check_output", lambda *_args: next(manifests))

    with pytest.raises(ValueError, match="image assembly is not reproducible"):
        nix_runner_image.build(tmp_path, tmp_path / "work", check_reproducible=True)


if __name__ == "__main__":
    pytest_bazel.main()
