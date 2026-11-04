"""Verify that locally imported images survive a retained k3s baseline."""

import json
import os
import uuid
from pathlib import Path

from agentplane.system_tests.k3s import stack
from agentplane.system_tests.k3s.stack import K3s
from devinfra.bazel.runfiles import get_required_path


def _assert_cri_pinned(k3s: K3s, reference: str, work: Path) -> None:
    config = work / "crictl.yaml"
    config.write_text(f"runtime-endpoint: unix://{stack.CONTAINERD_SOCKET}\n")
    images = json.loads(
        stack.run(str(k3s.binary), "crictl", "--config", str(config), "images", "-o", "json", stdout_only=True)
    )["images"]
    matches = [image for image in images if reference in image.get("repoTags", [])]
    assert len(matches) == 1, f"CRI image {reference}: {matches}"
    assert matches[0]["pinned"] is True, matches[0]


def test_imported_image_is_pinned_after_clean_attach(tmp_path: Path) -> None:
    binary = get_required_path(os.environ["K3S_BINARY_RLOCATION"])
    layout = get_required_path(os.environ["K3S_TEST_IMAGE_RLOCATION"])
    reference = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:unset"
    fingerprint = "pinned-image-probe-v1:" + uuid.uuid4().hex
    k3s, marker, warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
    try:
        assert not warm
        identity = marker["identity"].copy()
        k3s.import_images({reference: layout})
        _assert_cri_pinned(k3s, reference, tmp_path)

        # A successful suite leaves a clean marker. The next action must attach to the
        # same process and keep the CRI pin, including when it repeats the import.
        marker["state"] = "clean"
        stack._baseline_write_marker(marker)
        k3s, marker, warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
        assert warm
        assert marker["identity"] == identity
        _assert_cri_pinned(k3s, reference, tmp_path)
        k3s.import_images({reference: layout})
        _assert_cri_pinned(k3s, reference, tmp_path)
    finally:
        marker["state"] = "dirty"
        stack._baseline_write_marker(marker)
        try:
            stack._baseline_stop_owned(k3s, marker)
        finally:
            stack._BASELINE_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
