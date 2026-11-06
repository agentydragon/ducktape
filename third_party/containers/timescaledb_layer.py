"""Relocate the Timescale payload in a pinned OCI layout into a PostgreSQL image layer."""

from __future__ import annotations

import argparse
import copy
import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

_PAYLOAD_ROOTS = ("lib", "share")
_IMAGE_ROOT = "extensions/timescaledb-oss"


def _normalise_layer_path(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    normalized = PurePosixPath(path).as_posix().lstrip("/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"Unsafe Timescale payload path {path!r}")
    return normalized


def _payload_path(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in _PAYLOAD_ROOTS)


def _remove_path(entries: dict[str, tuple[tarfile.TarInfo, bytes | None]], path: str) -> None:
    for entry_path in tuple(entries):
        if entry_path == path or entry_path.startswith(f"{path}/"):
            del entries[entry_path]


def _add_layer(layout: Path, layer_digest: str, entries: dict[str, tuple[tarfile.TarInfo, bytes | None]]) -> None:
    layer_path = layout / "blobs" / layer_digest.replace(":", "/")
    with tarfile.open(layer_path, mode="r:*") as layer:
        for member in layer:
            path = _normalise_layer_path(member.name)
            if not path:
                continue

            parent, _, basename = path.rpartition("/")
            if basename == ".wh..wh..opq":
                if _payload_path(parent):
                    for entry_path in tuple(entries):
                        if entry_path.startswith(f"{parent}/"):
                            del entries[entry_path]
                continue
            if basename.startswith(".wh."):
                removed_path = f"{parent}/{basename[4:]}" if parent else basename[4:]
                if _payload_path(removed_path):
                    _remove_path(entries, removed_path)
                continue

            if not _payload_path(path):
                continue
            if member.isreg():
                content_file = layer.extractfile(member)
                if content_file is None:
                    raise ValueError(f"Could not read Timescale payload entry {member.name!r}")
                with content_file:
                    entries[path] = (member, content_file.read())
            elif member.isdir() or member.issym() or member.islnk():
                entries[path] = (member, None)
            else:
                raise ValueError(f"Unsupported Timescale payload entry {member.name!r}")


def _image_manifest(layout: Path) -> dict[str, Any]:
    index = json.loads((layout / "index.json").read_text())
    manifests = index["manifests"]
    if len(manifests) != 1:
        raise ValueError(f"Expected one platform image in {layout}, found {len(manifests)}")
    digest = manifests[0]["digest"]
    manifest_path = layout / "blobs" / digest.replace(":", "/")
    return cast(dict[str, Any], json.loads(manifest_path.read_text()))


def _directory_entry(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _relocated_link(linkname: str, *, hardlink: bool) -> str:
    target = _normalise_layer_path(linkname) if hardlink else linkname
    for root in _PAYLOAD_ROOTS:
        prefix = f"/{root}"
        if not hardlink and (target == prefix or target.startswith(f"{prefix}/")):
            return f"/{_IMAGE_ROOT}{target}"
        if hardlink and (target == root or target.startswith(f"{root}/")):
            return f"{_IMAGE_ROOT}/{target}"
    return target


def _write_layer(layout: Path, output: Path) -> None:
    entries: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    manifest = _image_manifest(layout)
    for layer in manifest["layers"]:
        _add_layer(layout, layer["digest"], entries)

    for root in _PAYLOAD_ROOTS:
        if root not in entries:
            entries[root] = (_directory_entry(root), None)
        elif not entries[root][0].isdir():
            raise ValueError(f"Timescale payload path {root!r} is not a directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, mode="w", format=tarfile.PAX_FORMAT) as layer:
        layer.addfile(_directory_entry("extensions"))
        layer.addfile(_directory_entry(_IMAGE_ROOT))
        for source_path, (source_info, content) in sorted(
            entries.items(), key=lambda item: (not item[1][0].isdir(), item[0])
        ):
            info = copy.copy(source_info)
            info.name = f"{_IMAGE_ROOT}/{source_path}"
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.pax_headers = {}
            if info.issym() or info.islnk():
                info.linkname = _relocated_link(info.linkname, hardlink=info.islnk())
            if content is None:
                layer.addfile(info)
            else:
                layer.addfile(info, io.BytesIO(content))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _write_layer(args.layout, args.output)


if __name__ == "__main__":
    main()
