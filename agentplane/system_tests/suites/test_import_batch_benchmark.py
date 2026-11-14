"""Manual RBE comparison of production serial imports and bounded OCI batches."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest_bazel
from python.runfiles import Runfiles
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.baseline import K3s
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

MAX_BATCH_IMAGES = 8
MAX_BATCH_BYTES = 256 * 1024 * 1024
CRI_PUBLICATION_SECONDS = 10
DEFAULT_ORDER = "serial,bounded_8_256"
IMPORT_ARMS = {"serial", "bounded_8_256"}


def _batches(images: dict[str, Path]) -> Iterator[tuple[list[str], list[dict[str, Any]], dict[str, Path], int]]:
    references: list[str] = []
    manifests: list[dict[str, Any]] = []
    blobs: dict[str, Path] = {}
    size_bytes = 0
    for reference, layout in images.items():
        descriptors = json.loads((layout / "index.json").read_text())["manifests"]
        for descriptor in descriptors:
            descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = reference
        image_blobs = {blob.relative_to(layout).as_posix(): blob for blob in (layout / "blobs").glob("*/*")}
        added_bytes = sum(image_blobs[name].stat().st_size for name in image_blobs.keys() - blobs.keys())
        if references and (len(references) >= MAX_BATCH_IMAGES or size_bytes + added_bytes > MAX_BATCH_BYTES):
            yield references, manifests, blobs, size_bytes
            references, manifests, blobs, size_bytes = [], [], {}, 0
            added_bytes = sum(blob.stat().st_size for blob in image_blobs.values())
        references.append(reference)
        manifests.extend(descriptors)
        for name, blob in image_blobs.items():
            blobs.setdefault(name, blob)
        size_bytes += added_bytes
    if references:
        yield references, manifests, blobs, size_bytes


def _candidate_import_images(k3s: K3s, images: dict[str, Path]) -> None:
    """Batch current-source imports while preserving usability, pinning and pruning policy."""
    k3s.protect_image_layouts(images)
    k3s.prune_unused_images()
    layers = k3s.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
    filesystem = os.statvfs(layers)
    prune_below_bytes = filesystem.f_bavail * filesystem.f_frsize // 2
    usable = k3s._usable_images(images)
    for reference in images:
        if reference in usable:
            with tracer.start_as_current_span("system_test.image.import", attributes={"image": reference}) as span:
                span.set_attribute("image.import.skipped", True)
    remaining = {reference: layout for reference, layout in images.items() if reference not in usable}
    for references, manifests, blobs, size_bytes in _batches(remaining):
        with tracer.start_as_current_span(
            "system_test.image.import",
            attributes={
                "image": ",".join(references),
                "image.count": len(references),
                "image.layout_bytes": size_bytes,
                "image.import.skipped": False,
            },
        ) as span:
            stored = k3s._stored_blobs()
            missing = {name: blobs[name] for name in blobs.keys() - stored}
            span.set_attribute("image.missing_blob_count", len(missing))
            k3s._import_layout(manifests, missing)
        if missing:
            filesystem = os.statvfs(layers)
            if filesystem.f_bavail * filesystem.f_frsize < prune_below_bytes:
                k3s.prune_unused_images()
    k3s.prune_unused_images()


def _arm_order() -> list[str]:
    order = os.environ.get("K3S_IMPORT_BENCHMARK_ORDER", DEFAULT_ORDER).split(",")
    if len(order) != 2 or set(order) != IMPORT_ARMS:
        raise ValueError(f"expected both import arms once, got {order}")
    return order


def _source_blob_paths(images: dict[str, Path]) -> set[str]:
    return {
        blob.relative_to(layout).as_posix() for layout in images.values() for blob in (layout / "blobs").glob("*/*")
    }


def _input_identity(groups: list[tuple[str, dict[str, Path]]]) -> dict[str, Any]:
    layouts: list[dict[str, Any]] = []
    unique_blob_sizes: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    for group, images in groups:
        group_counts[group] = len(images)
        for reference, layout in images.items():
            index_bytes = (layout / "index.json").read_bytes()
            descriptors = json.loads(index_bytes)["manifests"]
            if len(descriptors) != 1:
                raise ValueError(f"benchmark requires one OCI target manifest per tag: {reference}")
            target_digest = descriptors[0]["digest"]
            algorithm, encoded = target_digest.split(":", 1)
            target = json.loads((layout / "blobs" / algorithm / encoded).read_bytes())
            if "config" not in target:
                raise ValueError(f"benchmark input is not a single-platform image manifest: {reference}")
            blob_sizes = {
                blob.relative_to(layout).as_posix(): blob.stat().st_size for blob in (layout / "blobs").glob("*/*")
            }
            for name, size in blob_sizes.items():
                previous_size = unique_blob_sizes.setdefault(name, size)
                if previous_size != size:
                    raise ValueError(f"the same OCI blob path has conflicting sizes: {name}")
            layouts.append(
                {
                    "group": group,
                    "reference": reference,
                    "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
                    "manifest_count": len(descriptors),
                    "target_digest": target_digest,
                    "config_digest": target["config"]["digest"],
                    "blob_count": len(blob_sizes),
                    "blob_bytes": sum(blob_sizes.values()),
                    "blob_paths": sorted(blob_sizes),
                }
            )
    return {
        "image_count": len(layouts),
        "group_counts": group_counts,
        "unique_blob_count": len(unique_blob_sizes),
        "unique_blob_bytes": sum(unique_blob_sizes.values()),
        "layouts": layouts,
    }


def _warm_source_layouts(images: dict[str, Path]) -> None:
    """Give each import phase the same source-file cache treatment."""
    for layout in images.values():
        with (layout / "index.json").open("rb") as source:
            while source.read(1024 * 1024):
                pass
        for blob in sorted((layout / "blobs").glob("*/*")):
            with blob.open("rb") as source:
                while source.read(4 * 1024 * 1024):
                    pass


def _assert_cri_images_ready(k3s: K3s, expected_targets: dict[str, dict[str, Any]]) -> None:
    """Wait briefly for CRI publication, then check pins and exact unpacked targets directly."""
    # K3s._usable_images documents the CRI's asynchronous pin publication after import.
    filters = [
        f"name=={json.dumps(reference)},target.digest=={target['target_digest']},"
        'labels."io.cri-containerd.pinned"==pinned'
        for reference, target in expected_targets.items()
    ]
    for attempt in Retrying(
        stop=stop_after_delay(CRI_PUBLICATION_SECONDS),
        wait=wait_fixed(0.25),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            cri_images = json.loads(k3s.crictl("images", "-o", "json"))["images"]
            for reference, target in expected_targets.items():
                matches = [image for image in cri_images if reference in image.get("repoTags", [])]
                assert len(matches) == 1, f"expected one CRI tag for {reference}: {matches}"
                image = matches[0]
                assert image.get("pinned") is True, f"CRI tag is not pinned: {reference}"
                assert image.get("id") == target["config_digest"], (
                    f"CRI image ID does not match the OCI config digest for {reference}: "
                    f"{image.get('id')} != {target['config_digest']}"
                )
            ready = set(
                baseline.run(
                    str(k3s.binary),
                    "ctr",
                    "--address",
                    baseline.CONTAINERD_SOCKET,
                    "-n",
                    "k8s.io",
                    "images",
                    "check",
                    "--quiet",
                    "--snapshotter",
                    "overlayfs",
                    *filters,
                    stdout_only=True,
                ).splitlines()
            )
            missing = set(expected_targets) - ready
            assert not missing, f"pinned exact target digests are not unpacked: {sorted(missing)}"


def _assert_imported(
    k3s: K3s, images: dict[str, Path], source_blobs: set[str], expected_targets: dict[str, dict[str, Any]]
) -> dict[str, int]:
    k3s.prune_unused_images()
    stored = k3s._stored_blobs()
    missing_blobs = source_blobs - stored
    assert not missing_blobs, f"containerd is missing OCI blobs after prune: {sorted(missing_blobs)}"
    _assert_cri_images_ready(k3s, expected_targets)
    return {"oci_blobs_checked": len(source_blobs), "usable_tags_checked": len(images)}


def _run_import_arm(k3s: K3s, arm: str, groups: list[tuple[str, dict[str, Path]]], phase: str) -> None:
    with tracer.start_as_current_span(
        "system_test.image.import_arm", attributes={"image.arm": arm, "image.arm.phase": phase}
    ):
        for group_name, images in groups:
            with tracer.start_as_current_span(
                "system_test.image.import_group", attributes={"image.arm": arm, "image.group": group_name}
            ):
                if arm == "serial":
                    # This is the actual unmodified production implementation.
                    k3s.import_images(images)
                else:
                    assert arm == "bounded_8_256"
                    _candidate_import_images(k3s, images)


def _path_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path)}
    try:
        metadata = path.lstat()
    except OSError as error:
        result["lstat_error"] = f"{type(error).__name__}: {error}"
        return result
    result.update(
        {
            "mode": stat.filemode(metadata.st_mode),
            "size": metadata.st_size,
            "is_symlink": stat.S_ISLNK(metadata.st_mode),
            "is_regular_file": stat.S_ISREG(metadata.st_mode),
            "is_directory": stat.S_ISDIR(metadata.st_mode),
        }
    )
    if stat.S_ISLNK(metadata.st_mode):
        result["symlink_target"] = str(path.readlink())
        try:
            target_metadata = path.stat()
        except OSError as error:
            result["target_stat_error"] = f"{type(error).__name__}: {error}"
        else:
            result["target_is_regular_file"] = stat.S_ISREG(target_metadata.st_mode)
            result["target_mode"] = stat.filemode(target_metadata.st_mode)
    return result


def _directory_listing(path: Path) -> dict[str, Any]:
    try:
        entries = sorted(path.iterdir())
    except OSError as error:
        return {"path": str(path), "error": f"{type(error).__name__}: {error}"}
    return {"path": str(path), "entries": [_path_metadata(entry) for entry in entries]}


def _capture_start_failure(k3s: K3s, arm: str, error: BaseException) -> None:
    """Keep helper metadata and K3s/containerd logs without changing startup behavior."""
    output = undeclared_outputs_dir() / f"import-benchmark-start-failure-{arm}"
    output.mkdir(parents=True, exist_ok=True)
    helper = k3s.data_dir / "data/current/bin/aux/iptables"
    details: dict[str, Any] = {
        "error": f"{type(error).__name__}: {error}",
        "data_dir": str(k3s.data_dir),
        "helper": _path_metadata(helper),
        "data_current": _path_metadata(k3s.data_dir / "data/current"),
        "data_current_aux": _path_metadata(k3s.data_dir / "data/current/bin/aux"),
        "data_current_listing": _directory_listing(k3s.data_dir / "data/current"),
        "data_current_aux_listing": _directory_listing(k3s.data_dir / "data/current/bin/aux"),
        "copied_logs": {},
    }
    log_root = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", str(k3s.work)))
    log_paths = {
        "k3s.log": log_root / "k3s.log",
        "containerd-preserved.log": log_root / "containerd.log",
        "containerd-live.log": k3s.data_dir / "agent/containerd/containerd.log",
    }
    for name, source in log_paths.items():
        destination = output / name
        try:
            shutil.copyfile(source, destination)
        except OSError as copy_error:
            details["copied_logs"][name] = f"{type(copy_error).__name__}: {copy_error}"
        else:
            details["copied_logs"][name] = str(destination)
    (output / "start-failure.json").write_text(json.dumps(details, indent=2) + "\n")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def test_import_batch_arms_use_fresh_stores_and_warm_repeats(
    runfiles: Runfiles, k3s_binary: Path, tmp_path: Path
) -> None:
    runfiles_root = Path(str(runfiles.root()))
    manifest_path = runfiles_root / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]
    image_manifest = json.loads(manifest_path.read_text())
    groups = [
        (group, {reference: runfiles_root / layout for reference, layout in image_manifest[group].items()})
        for group in ("pki", "fixture")
    ]
    if not all(images for _, images in groups):
        raise ValueError("the current Pod profile must declare both pki and fixture images")
    all_images = {reference: layout for _, images in groups for reference, layout in images.items()}
    if len(all_images) != sum(len(images) for _, images in groups):
        raise ValueError("the current Pod profile reuses an image reference across import groups")
    input_identity = _input_identity(groups)
    expected_targets = {layout["reference"]: layout for layout in input_identity["layouts"]}
    source_blobs = _source_blob_paths(all_images)
    order = _arm_order()
    output = undeclared_outputs_dir() / "import-batch-benchmark.json"
    report: dict[str, Any] = {
        "status": "running",
        "data_dir": "/var/lib/k3s-env",
        "groups": [name for name, _ in groups],
        "input": input_identity,
        "arm_order": order,
        "phases": [],
        "measurement": (
            "Native system_test.image.import spans; source layouts are read before every phase. "
            "Each cold arm starts with a fresh containerd store; its warm repeat reuses that store."
        ),
    }
    _write_report(output, report)

    baseline.stop_previous()
    try:
        for position, arm in enumerate(order):
            work = tmp_path / arm
            work.mkdir()
            k3s = K3s(work, k3s_binary)
            if k3s.data_dir != Path("/var/lib/k3s-env"):
                raise ValueError(f"unexpected K3s data root: {k3s.data_dir}")
            process = None
            try:
                try:
                    process = k3s.start(layers_in_memory=True, preserve_image_store=False)
                except BaseException as error:
                    _capture_start_failure(k3s, arm, error)
                    raise

                preexisting = k3s._stored_blobs() & source_blobs
                if preexisting:
                    raise AssertionError(f"cold containerd store already has source blobs: {sorted(preexisting)}")
                _warm_source_layouts(all_images)
                _run_import_arm(k3s, arm, groups, "cold_store")
                cold_check = _assert_imported(k3s, all_images, source_blobs, expected_targets)
                report["phases"].append(
                    {
                        "arm": arm,
                        "order": position,
                        "phase": "cold_store",
                        "preexisting_source_blob_count": len(preexisting),
                        **cold_check,
                    }
                )
                _write_report(output, report)

                warm_blobs = k3s._stored_blobs() & source_blobs
                if warm_blobs != source_blobs:
                    raise AssertionError(
                        "warm repeat does not have every source blob from its cold arm: "
                        f"{len(warm_blobs)} of {len(source_blobs)}"
                    )
                _warm_source_layouts(all_images)
                _run_import_arm(k3s, arm, groups, "warm_repeat")
                warm_check = _assert_imported(k3s, all_images, source_blobs, expected_targets)
                report["phases"].append(
                    {
                        "arm": arm,
                        "order": position,
                        "phase": "warm_repeat",
                        "preexisting_source_blob_count": len(warm_blobs),
                        **warm_check,
                    }
                )
                _write_report(output, report)
            finally:
                if process is not None:
                    k3s.stop(process)
    except BaseException as error:
        report["status"] = "failed"
        report["failure"] = f"{type(error).__name__}: {error}"
        _write_report(output, report)
        raise
    report["status"] = "passed"
    _write_report(output, report)


if __name__ == "__main__":
    pytest_bazel.main()
