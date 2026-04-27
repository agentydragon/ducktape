"""Extract a subdirectory from a pulled OCI image's flattened rootfs.

Reads an OCI image layout (`index.json` + `blobs/sha256/*`), selects the
manifest for the requested platform, applies its layers in order with
later-wins semantics into a temporary rootfs, then copies the requested
subdirectory to the action's output directory.

Whiteouts are skipped: this script is meant for benchmark/test corpora,
not for faithful overlay reconstruction.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import tempfile
from pathlib import Path


def find_layout_root(input_paths: list[Path]) -> Path:
    """Locate the OCI layout root from a mix of file and directory inputs.

    Some Bazel rules expand an OCI layout into individual blob files (so we get
    `index.json` directly in the input list), while others pass it as a single
    tree-artifact directory. Handle both: if any input is `index.json`, its
    parent is the root; otherwise look for a directory containing `index.json`.
    """
    for path in input_paths:
        if path.name == "index.json":
            return path.parent
    for path in input_paths:
        if path.is_dir() and (path / "index.json").is_file():
            return path
    raise SystemExit(f"index.json not found in {len(input_paths)} input(s)")


def read_blob_json(blobs_dir: Path, digest: str) -> dict:
    algo, hex_digest = digest.split(":", 1)
    with (blobs_dir / algo / hex_digest).open(encoding="utf-8") as fh:
        result: dict = json.load(fh)
        return result


def select_platform_manifest(top: dict, platform: str, blobs_dir: Path) -> dict:
    """Resolve a single-platform manifest from either an image manifest or an index."""
    if "layers" in top:
        return top
    if "/" not in platform:
        raise SystemExit(f"--platform must be in OS/ARCH form (e.g. linux/amd64), got {platform!r}")
    os_name, arch = platform.split("/", 1)
    for entry in top.get("manifests", []):
        plat = entry.get("platform", {})
        if plat.get("os") == os_name and plat.get("architecture") == arch:
            return read_blob_json(blobs_dir, entry["digest"])
    raise SystemExit(f"No manifest found for platform {platform!r}")


def apply_layer(blobs_dir: Path, digest: str, rootfs: Path) -> None:
    algo, hex_digest = digest.split(":", 1)
    with tarfile.open(blobs_dir / algo / hex_digest, "r:*") as tar:
        members = [member for member in tar.getmembers() if ".wh." not in member.name.split("/")]
        # filter="data" rejects absolute paths, parent traversals, special
        # files, and other tar oddities. Even with digest pinning the layer
        # blob is opaque to us, so we sandbox extraction defensively.
        tar.extractall(rootfs, members=members, filter="data")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--subdir", required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    layout_root = find_layout_root(args.inputs)
    blobs_dir = layout_root / "blobs"
    index = json.loads((layout_root / "index.json").read_text())
    top_manifest = read_blob_json(blobs_dir, index["manifests"][0]["digest"])
    manifest = select_platform_manifest(top_manifest, args.platform, blobs_dir)

    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract_image_subdir_") as tmp:
        rootfs = Path(tmp) / "rootfs"
        rootfs.mkdir()
        for layer in manifest["layers"]:
            apply_layer(blobs_dir, layer["digest"], rootfs)

        source = rootfs / args.subdir.lstrip("/")
        if not source.is_dir():
            raise SystemExit(f"Subdir not found in rootfs: {args.subdir}")
        for entry in source.iterdir():
            destination = args.out / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, symlinks=True)
            else:
                shutil.copy2(entry, destination, follow_symlinks=False)


if __name__ == "__main__":
    main()
