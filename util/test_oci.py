from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel

from util import oci


def test_load_oci_image_reconciles_pin_and_daemon_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tag = f"ducktape/oci-policy-test:{uuid4().hex}"
    locations: dict[str, Path] = {}
    images: list[oci.OciImage] = []
    wanted: list[str] = []
    for name in ("first", "repinned"):
        layout = tmp_path / name
        layout.mkdir()
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        (layout / "index.json").write_text(json.dumps({"manifests": [{"digest": digest}]}))
        rloc = f"test-layouts/{name}"
        rloc_file = tmp_path / f"{name}.rloc"
        rloc_file.write_text(rloc)
        locations[str(rloc_file)] = rloc_file
        locations[rloc] = layout
        images.append(oci.OciImage(str(rloc_file), tag))
        wanted.append(digest)

    daemon_ids: dict[str, str] = {}
    loaded: list[str] = []
    monkeypatch.setattr(oci.runfiles, "get_required_path", lambda rloc: locations[rloc])

    def image_id(image_tag: str) -> str | None:
        return daemon_ids.get(image_tag)

    monkeypatch.setattr(oci, "daemon_image_id", image_id)

    def load(layout: Path, image_tag: str) -> None:
        digest = oci.read_oci_layout_digest(layout)
        loaded.append(digest)
        daemon_ids[image_tag] = f"docker-config:{digest}"

    monkeypatch.setattr(oci, "push_to_daemon", load)
    marker = oci._loaded_marker(tag)
    marker.unlink(missing_ok=True)
    try:
        oci.load_oci_image(images[0])
        assert daemon_ids[tag] == f"docker-config:{wanted[0]}"
        assert loaded == [wanted[0]]

        oci.load_oci_image(images[0])
        assert loaded == [wanted[0]]

        oci.load_oci_image(images[1])
        assert daemon_ids[tag] == f"docker-config:{wanted[1]}"
        assert loaded == wanted

        daemon_ids[tag] = "docker-config:external-retag"
        oci.load_oci_image(images[1])
        assert daemon_ids[tag] == f"docker-config:{wanted[1]}"
        assert loaded == [wanted[0], wanted[1], wanted[1]]

        del daemon_ids[tag]
        oci.load_oci_image(images[1])
        assert daemon_ids[tag] == f"docker-config:{wanted[1]}"
        assert loaded == [wanted[0], wanted[1], wanted[1], wanted[1]]
    finally:
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    pytest_bazel.main()
