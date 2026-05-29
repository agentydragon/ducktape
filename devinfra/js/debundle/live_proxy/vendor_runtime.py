from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from pathlib import Path

from devinfra.js.debundle.live_proxy.package_tree import assert_subpath_does_not_escape, resolve_package_subpath


@dataclass(frozen=True)
class VendorRuntimeEntry:
    chunk_id: str
    chunk_path: str
    entry_file: str
    file_path: Path
    mount_root: Path
    mounted_entry_file: str
    package: str
    subpath: str
    version: str
    generated_wrapper_path: Path | None = None


@dataclass(frozen=True)
class VendorRuntimeRequest:
    entry: VendorRuntimeEntry
    file_path: Path
    request_path: str
    request_suffix: str


@dataclass(frozen=True)
class PartialSwapEntry:
    package: str
    version: str
    subpath: str
    file_path: Path
    mount_root: Path
    mounted_subpath: str
    url_prefix: str


@dataclass(frozen=True)
class PartialSwapRequest:
    entry: PartialSwapEntry
    file_path: Path
    request_path: str
    request_suffix: str
    resolved_suffix: str


PARTIAL_SWAP_URL_PREFIX = "_partial_swap"


def load_vendor_resolution_manifest(manifest_path: Path) -> dict:
    # Opaque external JSON produced by the Rust vendor-swap pipeline; entries are
    # parsed into typed VendorRuntimeEntry objects in load_vendor_runtime_index.
    if not manifest_path or not manifest_path.exists():
        return {}
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"Vendor manifest must be a JSON object at {manifest_path}")
    return raw.get("full") or {}


def load_vendor_runtime_index(
    *, manifest_path: Path, package_roots: dict[str, Path] | None = None, packages_root: Path | None = None
) -> dict[str, VendorRuntimeEntry]:
    resolutions = load_vendor_resolution_manifest(manifest_path)
    manifest_dir = manifest_path.parent
    by_chunk_id: dict[str, VendorRuntimeEntry] = {}
    for resolution_chunk_path, entry in resolutions.items():
        chunk_path = entry.get("chunk_path") or resolution_chunk_path
        if not entry.get("package") or not entry.get("subpath") or not entry.get("version"):
            raise RuntimeError(
                f"Vendor resolution for {chunk_path} is missing package/version/subpath in {manifest_path}"
            )
        chunk_id = chunk_id_for_chunk_path(chunk_path)
        entry_file = resolve_vendor_manifest_entry_file(entry, chunk_path=chunk_path, manifest_path=manifest_path)
        wrapper_abs_path = (
            resolve_relative(manifest_dir, entry["generated_wrapper_path"])
            if entry.get("generated_wrapper_path")
            else None
        )
        file_path = wrapper_abs_path or resolve_package_subpath(
            entry["package"], entry["subpath"], package_roots=package_roots, packages_root=packages_root
        )
        mount_root = resolve_mount_root(file_path, entry_file)
        mounted_entry_file = normalize_mounted_relative_path(
            posixpath.relpath(file_path.as_posix(), mount_root.as_posix())
        )
        by_chunk_id[chunk_id] = VendorRuntimeEntry(
            chunk_id=chunk_id,
            chunk_path=chunk_path,
            entry_file=entry_file,
            file_path=file_path,
            mount_root=mount_root,
            mounted_entry_file=mounted_entry_file,
            package=entry["package"],
            subpath=entry["subpath"],
            version=entry["version"],
            generated_wrapper_path=wrapper_abs_path,
        )
    return by_chunk_id


def resolve_vendor_runtime_request(
    relative_path: str, vendor_runtime_index: dict[str, VendorRuntimeEntry] | None
) -> VendorRuntimeRequest | None:
    if not vendor_runtime_index:
        return None
    normalized_path = normalize_relative_path(relative_path)
    candidate_paths = (
        [normalized_path, normalized_path.removeprefix("app/")]
        if normalized_path.startswith("app/")
        else [normalized_path]
    )
    for candidate_path in candidate_paths:
        for entry in vendor_runtime_index.values():
            prefix = f"{entry.chunk_id}/"
            if not candidate_path.startswith(prefix):
                continue
            suffix = candidate_path[len(prefix) :]
            return VendorRuntimeRequest(
                entry=entry,
                file_path=resolve_vendor_mounted_path(entry, suffix),
                request_path=candidate_path,
                request_suffix=suffix,
            )
    return None


def load_partial_swap_runtime_index(
    *, manifest_path: Path, package_roots: dict[str, Path] | None = None, packages_root: Path | None = None
) -> dict[str, PartialSwapEntry]:
    by_package: dict[str, PartialSwapEntry] = {}
    if not manifest_path or not manifest_path.exists():
        return by_package
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"Partial-swap manifest must be a JSON object at {manifest_path}")
    for entry in (raw.get("partial") or {}).values():
        for package_name, package_entry in (entry.get("packages") or {}).items():
            if not package_entry.get("subpath") or not package_entry.get("version"):
                raise RuntimeError(
                    f"Partial-swap resolution for {package_name} is missing subpath/version in {manifest_path}"
                )
            file_path = resolve_package_subpath(
                package_name, package_entry["subpath"], package_roots=package_roots, packages_root=packages_root
            )
            mount_root = resolve_package_mount_root(
                package_name, package_roots=package_roots, packages_root=packages_root, file_path=file_path
            )
            mounted_subpath = normalize_mounted_relative_path(
                posixpath.relpath(file_path.as_posix(), mount_root.as_posix())
            )
            by_package[package_name] = PartialSwapEntry(
                package=package_name,
                version=package_entry["version"],
                subpath=package_entry["subpath"],
                file_path=file_path,
                mount_root=mount_root,
                mounted_subpath=mounted_subpath,
                url_prefix=f"{PARTIAL_SWAP_URL_PREFIX}/{package_name}",
            )
    return by_package


def build_partial_swap_import_map(partial_swap_index: dict[str, PartialSwapEntry], app_asset_prefix: str) -> dict:
    imports = {}
    for entry in partial_swap_index.values():
        imports[entry.package] = f"{app_asset_prefix}/{entry.url_prefix}/{entry.mounted_subpath}"
    return {"imports": imports}


def resolve_partial_swap_runtime_request(
    relative_path: str, partial_swap_index: dict[str, PartialSwapEntry] | None
) -> PartialSwapRequest | None:
    if not partial_swap_index:
        return None
    normalized_path = normalize_relative_path(relative_path)
    candidate_paths = (
        [normalized_path, normalized_path.removeprefix("app/")]
        if normalized_path.startswith("app/")
        else [normalized_path]
    )
    for candidate_path in candidate_paths:
        partial_prefix = f"{PARTIAL_SWAP_URL_PREFIX}/"
        if not candidate_path.startswith(partial_prefix):
            continue
        rest = candidate_path[len(partial_prefix) :]
        for entry in partial_swap_index.values():
            package_prefix = f"{entry.package}/"
            if not rest.startswith(package_prefix):
                continue
            suffix = rest[len(package_prefix) :]
            # The request suffix is the only attacker-controlled portion; once
            # it lexically stays inside the mounted root, the join is safe
            # regardless of where symlinks below the root point. The previous
            # implementation `.resolve()`d the joined path and compared with
            # `mount_root.resolve()`, which broke under Bazel's runfiles tree
            # because leaf files are symlinks back into `bin/node_modules`.
            assert_subpath_does_not_escape(
                entry.package, suffix, f"Partial-swap request escapes mounted root for {entry.package}: {suffix}"
            )
            file_path, resolved_suffix = resolve_partial_swap_asset_path(entry.mount_root, suffix)
            return PartialSwapRequest(
                entry=entry,
                file_path=file_path,
                request_path=candidate_path,
                request_suffix=suffix,
                resolved_suffix=resolved_suffix,
            )
    return None


def resolve_partial_swap_asset_path(mount_root: Path, suffix: str) -> tuple[Path, str]:
    exact = (mount_root / suffix).resolve()
    if is_existing_file(exact):
        return exact, suffix
    for ext in [".js", ".mjs", ".cjs"]:
        candidate = Path(f"{exact}{ext}")
        if is_existing_file(candidate):
            return candidate, f"{suffix}{ext}"
    for ext in [".js", ".mjs", ".cjs"]:
        candidate = (exact / f"index{ext}").resolve()
        if is_existing_file(candidate):
            return candidate, f"{suffix.rstrip('/')}/index{ext}"
    return exact, suffix


def is_existing_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file()
    except OSError:
        return False


def resolve_package_mount_root(
    package_name: str, *, package_roots: dict[str, Path] | None, packages_root: Path | None, file_path: Path
) -> Path:
    if package_roots and package_name in package_roots:
        return package_roots[package_name].resolve()
    if packages_root:
        return (packages_root / package_name).resolve()
    return file_path.parent.resolve()


def chunk_id_for_chunk_path(chunk_path: str) -> str:
    if not chunk_path.endswith(".js"):
        raise RuntimeError(f"Expected .js chunk path, got {chunk_path}")
    return chunk_path[: -len(".js")]


def normalize_relative_path(relative_path: str | None) -> str:
    return str(relative_path or "").split("?", 1)[0].lstrip("/\\")


def resolve_vendor_mounted_path(entry: VendorRuntimeEntry, suffix: str) -> Path:
    if suffix in {"", "."}:
        raise RuntimeError(f"Invalid vendor request path for {entry.chunk_id}: {suffix}")
    mounted_relative_path = alias_vendor_entry_path(entry, suffix)
    # Validate the mounted relative path before joining with the mount root.
    # We cannot use `Path.resolve()` to detect escapes after the join because
    # Bazel's runfiles tree contains symlinks that point back into
    # `bazel-out/.../bin/node_modules`, so a legitimate file inside the
    # package would appear to "escape" once resolved.
    assert_subpath_does_not_escape(
        entry.chunk_id,
        mounted_relative_path,
        f"Vendor request escapes mounted root for {entry.chunk_id}: {mounted_relative_path}",
    )
    return entry.mount_root / mounted_relative_path


def alias_vendor_entry_path(entry: VendorRuntimeEntry, suffix: str) -> str:
    if suffix == entry.entry_file:
        return entry.mounted_entry_file
    if suffix == "runtime.js" and is_root_mounted_entry_file(entry.entry_file):
        return entry.mounted_entry_file
    return suffix


def normalize_entry_file(entry_file: str | None) -> str:
    normalized = posixpath.normpath(str(entry_file or "").replace("\\", "/"))
    if normalized in {"", "."} or normalized.startswith("/") or ".." in normalized.split("/"):
        raise RuntimeError(f"Invalid vendor entry file: {entry_file}")
    return normalized


def resolve_vendor_manifest_entry_file(entry: dict, *, chunk_path: str, manifest_path: Path) -> str:
    if not isinstance(entry.get("entry_file"), str) or entry["entry_file"] == "":
        raise RuntimeError(f"Vendor resolution for {chunk_path} is missing entry_file in {manifest_path}")
    return normalize_entry_file(entry["entry_file"])


def normalize_mounted_relative_path(relative_path: str) -> str:
    normalized = posixpath.normpath(str(relative_path or "").replace("\\", "/"))
    if normalized in {"", "."} or normalized.startswith("/") or ".." in normalized.split("/"):
        raise RuntimeError(f"Invalid mounted vendor path: {relative_path}")
    return normalized


def resolve_mount_root(file_path: Path, entry_file: str) -> Path:
    entry_dir = posixpath.dirname(entry_file)
    if entry_dir in {"", "."}:
        return file_path.parent.resolve()
    depth = len(entry_dir.split("/"))
    root = file_path.parent
    for _ in range(depth):
        root = root.parent
    return root.resolve()


def is_root_mounted_entry_file(entry_file: str) -> bool:
    return "/" not in entry_file


def resolve_relative(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()
