"""Retained-node compatibility and ownership gates without starting Kubernetes."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.k3s import baseline


@pytest.fixture
def retained_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "retained"
    root.mkdir()
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", root / "server.json")
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    return root


def _image_layout(root: Path, name: str, manifests: list[dict[str, Any]], blobs: dict[str, bytes]) -> Path:
    layout = root / name
    (layout / "blobs").mkdir(parents=True)
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": manifests}))
    for blob_name, contents in blobs.items():
        blob = layout / blob_name
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(contents)
    return layout


def _manifest(digest: str, *, reference: str | None = None) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "digest": "sha256:" + digest * 64,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "size": 1,
    }
    if reference is not None:
        descriptor["annotations"] = {"org.opencontainers.image.ref.name": reference}
    return descriptor


@pytest.mark.parametrize("changed", ["binary", "crd", "release", "image", "helper", "layer_mode"])
def test_infrastructure_inputs_change_compatibility(tmp_path: Path, changed: str) -> None:
    binary = tmp_path / "k3s"
    crd = tmp_path / "crd.yaml"
    helper = tmp_path / "extension.py"
    layout = tmp_path / "image"
    layout.mkdir()
    for path in (binary, crd, helper, layout / "index.json"):
        path.write_text("before")
    releases: list[dict[str, Any]] = [{"kind": "Deployment", "metadata": {"name": "test-controller"}}]

    def fingerprint(*, layers_in_memory: bool = True) -> str:
        return baseline.fingerprint(
            binary,
            [crd],
            releases,
            {"test-infra:unset": layout},
            layers_in_memory=layers_in_memory,
            helpers={"extension.py": helper},
        )

    previous = fingerprint()
    match changed:
        case "binary" | "crd" | "helper" | "image":
            {"binary": binary, "crd": crd, "helper": helper, "image": layout / "index.json"}[changed].write_text(
                "after"
            )
        case "release":
            releases[0]["spec"] = {"replicas": 2}
        case "layer_mode":
            pass
    assert fingerprint(layers_in_memory=changed != "layer_mode") != previous


def test_fingerprint_is_stable_across_input_order(tmp_path: Path) -> None:
    binary = tmp_path / "k3s"
    binary.write_bytes(b"test binary")
    layouts = {}
    helpers = {}
    for name in ("a", "b"):
        layout = tmp_path / name
        layout.mkdir()
        (layout / "index.json").write_text(name)
        layouts[name] = layout
        helpers[name] = layout / "index.json"
    first = baseline.fingerprint(binary, [], [], layouts, layers_in_memory=True, helpers=helpers)
    assert (
        baseline.fingerprint(
            binary,
            [],
            [],
            dict(reversed(layouts.items())),
            layers_in_memory=True,
            helpers=dict(reversed(helpers.items())),
        )
        == first
    )


def test_matching_clean_baseline_is_marked_dirty_before_reuse(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {"pid": os.getpid(), "test": "owned-identity"}
    baseline.write_marker({"state": "clean", "fingerprint": "compatible", "identity": identity})
    monkeypatch.setattr(baseline, "_baseline_identity", lambda _node, _pid: identity)

    _, marker, warm = baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)

    assert warm
    assert marker["state"] == "dirty"
    assert json.loads((retained_root / "server.json").read_text())["state"] == "dirty"


@pytest.mark.parametrize(("state", "fingerprint"), [("dirty", "compatible"), ("clean", "different")])
def test_dirty_or_incompatible_baseline_cannot_attach(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch, state: str, fingerprint: str
) -> None:
    baseline.write_marker({"state": state, "fingerprint": fingerprint, "identity": {"pid": 123}})
    stopped = []

    def stop_owned(_node: baseline.K3s, marker: dict[str, Any]) -> None:
        stopped.append(marker)
        raise RuntimeError("test stops at cold rebuild boundary")

    monkeypatch.setattr(baseline, "stop_owned", stop_owned)
    with pytest.raises(RuntimeError, match="cold rebuild boundary"):
        baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)
    assert stopped[0]["state"] == state


def test_unhealthy_clean_baseline_cannot_attach(retained_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline.write_marker({"state": "clean", "fingerprint": "compatible", "identity": {"pid": 123}})
    stopped = []

    def unhealthy(_node: baseline.K3s, _pid: int) -> dict[str, str | int]:
        raise RuntimeError("API unavailable")

    def stop_owned(_node: baseline.K3s, marker: dict[str, Any]) -> None:
        stopped.append(marker)
        raise RuntimeError("test stops at cold rebuild boundary")

    monkeypatch.setattr(baseline, "_baseline_identity", unhealthy)
    monkeypatch.setattr(baseline, "stop_owned", stop_owned)
    with pytest.raises(RuntimeError, match="cold rebuild boundary"):
        baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)
    assert len(stopped) == 1


def test_reused_pid_cannot_be_stopped(retained_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = baseline.K3s(retained_root, retained_root / "unused-binary")
    monkeypatch.setattr(baseline, "_baseline_process_start", lambda _pid: "new-start-ticks")
    previous = {
        "identity": {
            "pid": os.getpid(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "process_start_ticks": "old-start-ticks",
        }
    }
    with pytest.raises(RuntimeError, match="PID was reused"):
        baseline.stop_owned(node, previous)


def test_image_import_batches_preserve_multimanifest_refs_and_deduplicate_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = "blobs/sha256/" + "d" * 64
    unique_a = "blobs/sha256/" + "e" * 64
    unique_b = "blobs/sha256/" + "f" * 64
    first = _image_layout(tmp_path, "first", [_manifest("a"), _manifest("b")], {shared: b"shared", unique_a: b"a"})
    second = _image_layout(
        tmp_path, "second", [_manifest("c", reference="stale:tag")], {shared: b"shared", unique_b: b"b"}
    )
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_BYTES", 100)
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_IMAGES", 8)

    batches = list(baseline._image_import_batches({"fixture:first": first, "fixture:second": second}))

    assert len(batches) == 1
    references, manifests, blobs, size_bytes = batches[0]
    assert references == ["fixture:first", "fixture:second"]
    assert [descriptor["digest"] for descriptor in manifests] == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
    ]
    assert [descriptor["annotations"]["org.opencontainers.image.ref.name"] for descriptor in manifests] == [
        "fixture:first",
        "fixture:first",
        "fixture:second",
    ]
    assert set(blobs) == {shared, unique_a, unique_b}
    assert size_bytes == sum(len(contents) for contents in (b"shared", b"a", b"b"))


def test_image_import_batches_respect_the_image_count_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    images = {
        f"fixture:{name}": _image_layout(
            tmp_path, name, [_manifest(digest)], {"blobs/sha256/" + digest * 64: digest.encode()}
        )
        for name, digest in [("first", "a"), ("second", "b"), ("third", "c")]
    }
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_BYTES", 100)
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_IMAGES", 2)

    batches = list(baseline._image_import_batches(images))

    assert [batch[0] for batch in batches] == [["fixture:first", "fixture:second"], ["fixture:third"]]


def test_import_images_retargets_a_reference_in_a_later_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _image_layout(tmp_path, "first", [_manifest("a")], {"blobs/sha256/" + "a" * 64: b"first"})
    second = _image_layout(tmp_path, "second", [_manifest("b")], {"blobs/sha256/" + "b" * 64: b"second"})
    reference = "fixture:mutable"
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    node.data_dir = tmp_path / "k3s"
    layers = node.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
    layers.mkdir(parents=True)
    stored: set[str] = set()
    imports: list[tuple[list[dict[str, Any]], dict[str, Path]]] = []
    prunes: list[None] = []

    def import_layout(manifests: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        imports.append((manifests, blobs.copy()))
        stored.update(blobs)

    monkeypatch.setattr(node, "protect_image_layouts", lambda _images: None)
    monkeypatch.setattr(node, "prune_unused_images", lambda: prunes.append(None))
    monkeypatch.setattr(node, "_stored_blobs", stored.copy)
    monkeypatch.setattr(node, "_import_layout", import_layout)
    monkeypatch.setattr(baseline.os, "statvfs", lambda _path: SimpleNamespace(f_bavail=1000, f_frsize=1))

    node.import_images({reference: first})
    node.import_images({reference: second})

    assert [item[0][0]["digest"] for item in imports] == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    assert [item[0][0]["annotations"]["org.opencontainers.image.ref.name"] for item in imports] == [
        reference,
        reference,
    ]
    assert set(imports[1][1]) == {"blobs/sha256/" + "b" * 64}
    assert len(prunes) == 4


def test_import_images_lists_once_per_bounded_group_and_prunes_at_low_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = "blobs/sha256/" + "d" * 64
    first = _image_layout(
        tmp_path, "first", [_manifest("a"), _manifest("b")], {shared: b"shared", "blobs/sha256/" + "e" * 64: b"a"}
    )
    second = _image_layout(tmp_path, "second", [_manifest("c")], {shared: b"shared", "blobs/sha256/" + "f" * 64: b"b"})
    third = _image_layout(tmp_path, "third", [_manifest("g")], {shared: b"shared", "blobs/sha256/" + "1" * 64: b"cccc"})
    images = {"fixture:first": first, "fixture:second": second, "fixture:third": third}
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    node.data_dir = tmp_path / "k3s"
    layers = node.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
    layers.mkdir(parents=True)
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_BYTES", 10)
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_IMAGES", 8)

    protected: list[dict[str, Path]] = []
    prunes: list[None] = []
    events: list[str] = []
    stored: set[str] = set()
    listings: list[set[str]] = []
    imports: list[tuple[list[dict[str, Any]], dict[str, Path]]] = []

    def protect(current: dict[str, Path]) -> None:
        protected.append(current.copy())
        events.append("protect")

    def prune() -> None:
        prunes.append(None)
        events.append("prune")

    def stored_blobs() -> set[str]:
        listings.append(stored.copy())
        return stored.copy()

    def import_layout(manifests: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        imports.append((manifests, blobs.copy()))
        stored.update(blobs)

    free_space = iter([1000, 400, 1000])

    def statvfs(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(f_bavail=next(free_space), f_frsize=1)

    monkeypatch.setattr(node, "protect_image_layouts", protect)
    monkeypatch.setattr(node, "prune_unused_images", prune)
    monkeypatch.setattr(node, "_stored_blobs", stored_blobs)
    monkeypatch.setattr(node, "_import_layout", import_layout)
    monkeypatch.setattr(baseline.os, "statvfs", statvfs)

    node.import_images(images)

    assert protected == [images]
    assert events[:2] == ["protect", "prune"]
    assert len(listings) == 2
    assert len(imports) == 2
    assert [len(imported[0]) for imported in imports] == [3, 1]
    assert set(imports[0][1]) == {shared, "blobs/sha256/" + "e" * 64, "blobs/sha256/" + "f" * 64}
    assert set(imports[1][1]) == {"blobs/sha256/" + "1" * 64}
    assert len(prunes) == 3


def test_import_images_stops_after_a_failed_batch_and_propagates_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _image_layout(tmp_path, "first", [_manifest("a")], {"blobs/sha256/" + "a" * 64: b"a"})
    second = _image_layout(tmp_path, "second", [_manifest("b")], {"blobs/sha256/" + "b" * 64: b"b"})
    images = {"fixture:first": first, "fixture:second": second}
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    node.data_dir = tmp_path / "k3s"
    layers = node.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
    layers.mkdir(parents=True)
    monkeypatch.setattr(baseline, "IMAGE_IMPORT_BATCH_MAX_IMAGES", 1)

    protected: list[dict[str, Path]] = []
    prunes: list[None] = []
    listings: list[set[str]] = []
    imports: list[list[dict[str, Any]]] = []

    def protect(current: dict[str, Path]) -> None:
        protected.append(current.copy())

    def prune() -> None:
        prunes.append(None)

    def stored_blobs() -> set[str]:
        listings.append(set())
        return set()

    def fail_import(manifests: list[dict[str, Any]], _blobs: dict[str, Path]) -> None:
        imports.append(manifests)
        raise RuntimeError("containerd image import exited 1")

    monkeypatch.setattr(node, "protect_image_layouts", protect)
    monkeypatch.setattr(node, "prune_unused_images", prune)
    monkeypatch.setattr(node, "_stored_blobs", stored_blobs)
    monkeypatch.setattr(node, "_import_layout", fail_import)

    with pytest.raises(RuntimeError, match="containerd image import exited 1"):
        node.import_images(images)

    assert protected == [images]
    assert len(listings) == 1
    assert len(imports) == 1
    assert len(prunes) == 1


if __name__ == "__main__":
    pytest_bazel.main()
