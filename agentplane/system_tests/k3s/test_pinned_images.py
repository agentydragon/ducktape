"""Verify that locally imported images survive a retained k3s baseline."""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from python.runfiles import Runfiles

from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.baseline import K3s


def _assert_cri_pinned(k3s: K3s, reference: str, work: Path) -> str:
    config = work / "crictl.yaml"
    config.write_text(f"runtime-endpoint: unix://{baseline.CONTAINERD_SOCKET}\n")
    images = json.loads(
        baseline.run(str(k3s.binary), "crictl", "--config", str(config), "images", "-o", "json", stdout_only=True)
    )["images"]
    matches = [image for image in images if reference in image.get("repoTags", [])]
    assert len(matches) == 1, f"CRI image {reference}: {matches}"
    assert matches[0]["pinned"] is True, matches[0]
    return str(matches[0]["id"])


def test_imported_image_is_pinned_after_clean_attach(
    runfiles: Runfiles, k3s_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = runfiles.root() / os.environ["K3S_TEST_IMAGE_RLOCATION"]
    reference = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:unset"
    fingerprint = "pinned-image-probe-v1:" + uuid.uuid4().hex
    k3s, marker, warm = baseline.open_baseline(k3s_binary, fingerprint, layers_in_memory=True)
    try:
        assert not warm
        identity = marker["identity"].copy()
        k3s.import_images({reference: layout})
        image_id = _assert_cri_pinned(k3s, reference, tmp_path)
        assert k3s._usable_images({reference: layout}) == {reference}

        def reject_transaction(_node: K3s, _manifests: list[dict[str, Any]], _blobs: dict[str, Path]) -> None:
            raise AssertionError("usable image reopened an import transaction")

        with monkeypatch.context() as context:
            context.setattr(K3s, "_import_layout", reject_transaction)
            k3s.import_images({reference: layout})

        # A pin observed on an old CRI config behind this tag cannot certify a new target.
        with monkeypatch.context() as context:
            context.setattr(
                k3s,
                "crictl",
                lambda *_args, **_kwargs: json.dumps(
                    {"images": [{"id": "sha256:stale", "repoTags": [reference], "pinned": True}]}
                ),
            )
            assert not k3s._usable_images({reference: layout})

        # Model the transient CRI inventory between creating the ID alias and publishing
        # the pinned tag. A real CRI RemoveImage on that ID would remove the tag too.
        crictl = k3s.crictl

        def stale_inventory(*args: str, **kwargs: Any) -> str:
            if args == ("images", "-o", "json"):
                return json.dumps({"images": [{"id": image_id}]})
            return crictl(*args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(k3s, "crictl", stale_inventory)
            k3s.prune_unused_images()
        _assert_cri_pinned(k3s, reference, tmp_path)

        # A successful suite leaves a clean marker. The next action must attach to the
        # same process and keep the CRI pin, including when it repeats the import.
        marker["state"] = "clean"
        baseline.write_marker(marker)
        k3s, marker, warm = baseline.open_baseline(k3s_binary, fingerprint, layers_in_memory=True)
        assert warm
        assert marker["identity"] == identity
        _assert_cri_pinned(k3s, reference, tmp_path)
        k3s.import_images({reference: layout})
        _assert_cri_pinned(k3s, reference, tmp_path)
        assert k3s._usable_images({reference: layout}) == {reference}

        # A new manifest behind the same fixture tag must still enter the importer.
        updated = tmp_path / "retagged-layout"
        shutil.copytree(layout, updated, symlinks=True)
        index = json.loads((layout / "index.json").read_text())
        original_digest = index["manifests"][0]["digest"].split(":", 1)[1]
        manifest = json.loads((layout / "blobs/sha256" / original_digest).read_text())
        manifest["annotations"] = {"probe": "same-tag-new-manifest"}
        encoded = json.dumps(manifest).encode()
        changed_digest = hashlib.sha256(encoded).hexdigest()
        (updated / "blobs/sha256" / changed_digest).write_bytes(encoded)
        index["manifests"][0]["digest"] = "sha256:" + changed_digest
        index["manifests"][0]["size"] = len(encoded)
        (updated / "index.json").unlink()
        (updated / "index.json").write_text(json.dumps(index))
        assert not k3s._usable_images({reference: updated})
        k3s.import_images({reference: updated})
        assert k3s._usable_images({reference: updated}) == {reference}
        assert not k3s._usable_images({reference: layout})
        k3s.import_images({reference: layout})

        # Reset the control plane while retaining containerd's store, then verify the
        # native check again. No process-local memoization can satisfy this assertion.
        baseline.stop_owned(k3s, marker)
        baseline._BASELINE_MARKER.unlink()
        k3s, marker, warm = baseline.open_baseline(k3s_binary, fingerprint, layers_in_memory=True)
        assert not warm
        assert k3s._usable_images({reference: layout}) == {reference}

        ctr = [str(k3s.binary), "ctr", "--address", baseline.CONTAINERD_SOCKET, "-n", "k8s.io"]
        baseline.run(*ctr, "images", "label", reference, "io.cri-containerd.pinned=")
        assert not k3s._usable_images({reference: layout})
        k3s.import_images({reference: layout})
        assert k3s._usable_images({reference: layout}) == {reference}

        # Remove the committed root snapshot while retaining the tag and all content.
        # Blob presence must not suppress the importer that reconstructs the rootfs.
        index = json.loads((layout / "index.json").read_text())
        digest = index["manifests"][0]["digest"].split(":", 1)[1]
        manifest = json.loads((layout / "blobs/sha256" / digest).read_text())
        config_id = manifest["config"]["digest"].split(":", 1)[1]
        config = json.loads((layout / "blobs/sha256" / config_id).read_text())
        chain = config["rootfs"]["diff_ids"][0]
        for diff_id in config["rootfs"]["diff_ids"][1:]:
            chain = "sha256:" + hashlib.sha256(f"{chain} {diff_id}".encode()).hexdigest()
        baseline.run(*ctr, "snapshots", "--snapshotter", "overlayfs", "remove", chain)
        assert not k3s._usable_images({reference: layout})
        k3s.import_images({reference: layout})
        assert k3s._usable_images({reference: layout}) == {reference}

        # CRI RemoveImage is the same deletion boundary used by kubelet image GC.
        k3s.crictl("rmi", reference)
        assert not k3s._usable_images({reference: layout})
        k3s.import_images({reference: layout})
        assert k3s._usable_images({reference: layout}) == {reference}
    finally:
        marker["state"] = "dirty"
        baseline.write_marker(marker)
        try:
            baseline.stop_owned(k3s, marker)
        finally:
            baseline._BASELINE_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
