from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.oci import oci


def test_load_oci_image_uses_manifest_digest_and_respects_explicit_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digests: list[str] = []
    for name in ("first", "repinned"):
        layout = tmp_path / "test-layouts" / name
        layout.mkdir(parents=True)
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        (layout / "index.json").write_text(json.dumps({"manifests": [{"digest": digest}]}))
        digests.append(digest)

    daemon_ids: dict[str, str] = {}
    loaded: list[tuple[str, str]] = []
    monkeypatch.setattr(oci.Runfiles, "CreateOrRaise", lambda: oci.Runfiles.CreateDirectoryBased(str(tmp_path)))
    monkeypatch.setattr(oci, "daemon_image_id", daemon_ids.get)
    monkeypatch.setattr(
        oci, "_loaded_marker", lambda ref: tmp_path / f"{hashlib.sha256(ref.encode()).hexdigest()}.loaded"
    )
    monkeypatch.setattr(oci, "_lock_path", lambda ref: tmp_path / f"{hashlib.sha256(ref.encode()).hexdigest()}.lock")

    def load(layout: Path, image_ref: str) -> None:
        digest = oci.read_oci_layout_digest(layout)
        loaded.append((image_ref, digest))
        daemon_ids[image_ref] = f"docker-config:{digest}"

    monkeypatch.setattr(oci, "push_to_daemon", load)
    first_ref = f"ducktape/oci:{digests[0].replace(':', '-')}"
    second_ref = f"ducktape/oci:{digests[1].replace(':', '-')}"
    assert oci.load_oci_image("test-layouts/first") == first_ref
    assert oci.load_oci_image("test-layouts/first") == first_ref
    assert oci.load_oci_image("test-layouts/repinned") == second_ref
    assert loaded == [(first_ref, digests[0]), (second_ref, digests[1])]

    # A consumer can require a stable name; repinning, retagging, and pruning must reload it.
    explicit_ref = "ducktape/oci-policy-test:explicit"
    assert oci.load_oci_image("test-layouts/first", image_ref=explicit_ref) == explicit_ref
    assert oci.load_oci_image("test-layouts/first", image_ref=explicit_ref) == explicit_ref
    assert oci.load_oci_image("test-layouts/repinned", image_ref=explicit_ref) == explicit_ref
    assert loaded == [
        (first_ref, digests[0]),
        (second_ref, digests[1]),
        (explicit_ref, digests[0]),
        (explicit_ref, digests[1]),
    ]
    daemon_ids[explicit_ref] = "docker-config:external-retag"
    oci.load_oci_image("test-layouts/repinned", image_ref=explicit_ref)
    del daemon_ids[explicit_ref]
    oci.load_oci_image("test-layouts/repinned", image_ref=explicit_ref)
    assert loaded[-2:] == [(explicit_ref, digests[1]), (explicit_ref, digests[1])]


if __name__ == "__main__":
    pytest_bazel.main()
