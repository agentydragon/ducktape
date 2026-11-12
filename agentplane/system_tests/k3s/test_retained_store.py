"""Run real k3s processes to verify image-store reuse and rejection paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, cast

import pytest_bazel
from python.runfiles import Runfiles

from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.baseline import K3s
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_IMAGE_REFERENCE = "git.allegedly.works/ducktape-ci/retained-store-probe:v1"


def _probe_digests(layout: Path) -> set[str]:
    index = json.loads((layout / "index.json").read_text())
    descriptor = index["manifests"][0]
    algorithm, digest = descriptor["digest"].split(":", 1)
    manifest = json.loads((layout / "blobs" / algorithm / digest).read_text())
    return {descriptor["digest"], manifest["config"]["digest"], *(layer["digest"] for layer in manifest["layers"])}


def _stored_digests(k3s: K3s) -> set[str]:
    output = baseline.run(
        str(k3s.binary), "ctr", "--address", baseline.CONTAINERD_SOCKET, "-n", "k8s.io", "content", "ls", "--quiet"
    )
    return set(output.split())


def _image_is_present(k3s: K3s) -> bool:
    images = json.loads(k3s.crictl("images", "-o", "json"))["images"]
    return any(_IMAGE_REFERENCE in image.get("repoTags", []) for image in images)


def _cluster_identity(k3s: K3s) -> dict[str, str]:
    namespace = json.loads(k3s.kubectl("get", "namespace", "kube-system", "-o", "json", stdout_only=True))
    node = json.loads(k3s.kubectl("get", "node", "env", "-o", "json", stdout_only=True))
    return {"kube_system_namespace_uid": namespace["metadata"]["uid"], "node_uid": node["metadata"]["uid"]}


def _new_k3s(work: Path, binary: Path, data_dir: Path) -> K3s:
    work.mkdir(parents=True)
    k3s = K3s(work, binary)
    k3s.data_dir = data_dir
    return k3s


def _start(k3s: K3s, *, preserve_image_store: bool) -> subprocess.Popen[bytes]:
    process = k3s.start(layers_in_memory=False, preserve_image_store=preserve_image_store)
    return cast(subprocess.Popen[bytes], process)


def _binary_sha256(path: Path) -> str:
    with path.open("rb") as binary:
        return hashlib.file_digest(binary, "sha256").hexdigest()


def test_clean_image_store_reuse_and_rejected_reuse_paths(runfiles: Runfiles, k3s_binary: Path, tmp_path: Path) -> None:
    work_root = tmp_path / "retained-store"
    retained_root = Path("/var/lib/k3s-env")
    retained_root.mkdir(parents=True, exist_ok=True)
    data_dir = retained_root / f"retained-store-acceptance-{uuid.uuid4().hex}"
    data_dir.mkdir()
    disk_usage = shutil.disk_usage(data_dir)
    layout_manifest = json.loads((runfiles.root() / os.environ["IMAGE_MANIFEST_RLOCATION"]).read_text())
    layout = runfiles.root() / layout_manifest["fixture"][_IMAGE_REFERENCE]
    expected_digests = _probe_digests(layout)
    proof: dict[str, Any] = {
        "image_reference": _IMAGE_REFERENCE,
        "retention_path": str(data_dir),
        "disk_usage_before_k3s": {
            "free_bytes": disk_usage.free,
            "total_bytes": disk_usage.total,
            "used_bytes": disk_usage.used,
        },
        "source_blob_digests": sorted(expected_digests),
        "k3s_binary_sha256": _binary_sha256(k3s_binary),
    }
    active: tuple[K3s, subprocess.Popen[bytes]] | None = None

    def assert_image_present(k3s: K3s) -> set[str]:
        assert _image_is_present(k3s), f"containerd image reference {_IMAGE_REFERENCE} is absent"
        stored = _stored_digests(k3s)
        assert expected_digests <= stored, f"missing image content digests: {sorted(expected_digests - stored)}"
        return stored

    def assert_image_absent(k3s: K3s) -> set[str]:
        assert not _image_is_present(k3s), f"containerd image reference {_IMAGE_REFERENCE} survived a cold reset"
        stored = _stored_digests(k3s)
        assert expected_digests.isdisjoint(stored), (
            f"image blobs survived a cold reset: {sorted(expected_digests & stored)}"
        )
        return stored

    def stop_cleanly(k3s: K3s, process: subprocess.Popen[bytes]) -> None:
        nonlocal active
        k3s.stop(process)
        active = None

    try:
        cold = _new_k3s(work_root / "cold", k3s_binary, data_dir)
        assert not cold.image_store_is_retainable()
        cold_process = _start(cold, preserve_image_store=False)
        active = (cold, cold_process)
        cold_identity = _cluster_identity(cold)
        cold.import_images({_IMAGE_REFERENCE: layout})
        cold_stored = assert_image_present(cold)
        proof["cold_import"] = {
            "control_plane": cold_identity,
            "image_present": True,
            "source_digests_present": sorted(expected_digests & cold_stored),
        }
        stop_cleanly(cold, cold_process)

        warm = _new_k3s(work_root / "warm", k3s_binary, data_dir)
        warm_retainable = warm.image_store_is_retainable()
        assert warm_retainable, "clean same-binary image store was rejected on the RBE worker"
        warm_process = _start(warm, preserve_image_store=warm_retainable)
        active = (warm, warm_process)
        warm_identity = _cluster_identity(warm)
        warm_stored = assert_image_present(warm)
        assert warm_identity["kube_system_namespace_uid"] != cold_identity["kube_system_namespace_uid"]
        assert warm_identity["node_uid"] != cold_identity["node_uid"]
        proof["clean_reuse"] = {
            "policy_accepted": warm_retainable,
            "control_plane": warm_identity,
            "new_control_plane": True,
            "image_present_without_reimport": True,
            "source_digests_present": sorted(expected_digests & warm_stored),
        }
        stop_cleanly(warm, warm_process)

        interrupted = _new_k3s(work_root / "interrupted", k3s_binary, data_dir)
        interrupted_retainable = interrupted.image_store_is_retainable()
        assert interrupted_retainable
        interrupted_process = _start(interrupted, preserve_image_store=interrupted_retainable)
        active = (interrupted, interrupted_process)
        interrupted_identity = _cluster_identity(interrupted)
        assert_image_present(interrupted)
        interrupted_process.send_signal(signal.SIGKILL)
        interrupted_process.wait(timeout=baseline.STOP_TIMEOUT_SECONDS)
        interrupted.cleanup_stopped()
        active = None
        dirty_retainable = interrupted.image_store_is_retainable()
        assert not dirty_retainable, "abruptly terminated k3s run was accepted for image-store reuse"

        dirty_rebuild = _new_k3s(work_root / "dirty-rebuild", k3s_binary, data_dir)
        dirty_process = _start(dirty_rebuild, preserve_image_store=dirty_retainable)
        active = (dirty_rebuild, dirty_process)
        dirty_identity = _cluster_identity(dirty_rebuild)
        dirty_stored = assert_image_absent(dirty_rebuild)
        assert dirty_identity["kube_system_namespace_uid"] != interrupted_identity["kube_system_namespace_uid"]
        assert dirty_identity["node_uid"] != interrupted_identity["node_uid"]
        proof["abrupt_exit_reset"] = {
            "signal": "SIGKILL",
            "policy_accepted": dirty_retainable,
            "image_present_before_exit": True,
            "image_present_after_rebuild": False,
            "control_plane": dirty_identity,
            "new_control_plane": True,
            "retained_source_digests": sorted(expected_digests & dirty_stored),
        }
        dirty_rebuild.import_images({_IMAGE_REFERENCE: layout})
        assert_image_present(dirty_rebuild)
        stop_cleanly(dirty_rebuild, dirty_process)

        alternate_binary = work_root / "k3s-with-different-identity"
        shutil.copyfile(k3s_binary, alternate_binary)
        with alternate_binary.open("ab") as output:
            output.write(b"\n")
        alternate_binary.chmod(stat.S_IMODE(k3s_binary.stat().st_mode))
        assert _binary_sha256(alternate_binary) != _binary_sha256(k3s_binary)
        baseline.run(str(alternate_binary), "--version")

        mismatch = _new_k3s(work_root / "binary-mismatch", alternate_binary, data_dir)
        mismatch_retainable = mismatch.image_store_is_retainable()
        assert not mismatch_retainable, "image store from another K3s binary identity was accepted"
        mismatch_process = _start(mismatch, preserve_image_store=mismatch_retainable)
        active = (mismatch, mismatch_process)
        mismatch_identity = _cluster_identity(mismatch)
        mismatch_stored = assert_image_absent(mismatch)
        assert mismatch_identity["kube_system_namespace_uid"] != dirty_identity["kube_system_namespace_uid"]
        assert mismatch_identity["node_uid"] != dirty_identity["node_uid"]
        proof["binary_identity_reset"] = {
            "policy_accepted": mismatch_retainable,
            "base_sha256": _binary_sha256(k3s_binary),
            "alternate_sha256": _binary_sha256(alternate_binary),
            "alternate_binary_runs": True,
            "image_present_after_rebuild": False,
            "control_plane": mismatch_identity,
            "new_control_plane": True,
            "retained_source_digests": sorted(expected_digests & mismatch_stored),
        }
        stop_cleanly(mismatch, mismatch_process)
    finally:
        try:
            if active is not None:
                k3s, process = active
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=baseline.STOP_TIMEOUT_SECONDS)
                k3s.cleanup_stopped()
            if data_dir.exists():
                baseline._reset_previous_stack(data_dir)
                shutil.rmtree(data_dir)
        finally:
            artifact = undeclared_outputs_dir() / "retained-store-proof.json"
            artifact.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    pytest_bazel.main()
