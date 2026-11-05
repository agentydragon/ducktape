"""Verify that locally imported images survive a retained k3s baseline."""

import json
import os
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
