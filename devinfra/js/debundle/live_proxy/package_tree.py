from __future__ import annotations

import os
import posixpath
from pathlib import Path


def default_packages_root() -> Path:
    for runfiles_dir in [os.environ.get("RUNFILES_DIR"), os.environ.get("TEST_SRCDIR")]:
        if not runfiles_dir:
            continue
        for candidate in [Path(runfiles_dir) / "_main" / "node_modules", Path(runfiles_dir) / "node_modules"]:
            if candidate.exists():
                return candidate
    raise RuntimeError("Could not locate Bazel-provided package tree; pass packages_root explicitly")


def resolve_package_root(
    package_name: str, *, package_roots: dict[str, Path] | None = None, packages_root: Path | None = None
) -> Path:
    mapped_root = package_roots.get(package_name) if package_roots else None
    if mapped_root is not None:
        resolved_package_root = mapped_root.resolve()
        if not resolved_package_root.exists():
            raise RuntimeError(f"Package root not found for {package_name}: {resolved_package_root}")
        return resolved_package_root
    if package_roots and packages_root is None:
        raise RuntimeError(f"Package root not provided for {package_name}")

    resolved_packages_root = (packages_root or default_packages_root()).resolve()
    package_root = resolved_packages_root.joinpath(*package_path_segments(package_name))
    assert_subpath_does_not_escape(
        package_name, "/".join(package_path_segments(package_name)), f"Package {package_name} escapes packages root"
    )
    package_root = package_root.resolve()
    if not package_root.exists():
        raise RuntimeError(f"Package root not found for {package_name}: {package_root}")
    return package_root


def resolve_package_subpath(
    package_name: str,
    subpath: str,
    *,
    package_root: Path | None = None,
    package_roots: dict[str, Path] | None = None,
    packages_root: Path | None = None,
) -> Path:
    resolved_package_root = package_root or resolve_package_root(
        package_name, package_roots=package_roots, packages_root=packages_root
    )
    # Reject subpaths that try to escape the package root via parent-dir
    # references BEFORE joining with the root. We cannot rely on
    # `Path.resolve()` to detect escapes after joining, because Bazel's
    # runfiles tree materializes package directories as real directories whose
    # files are symlinks back to the original `bazel-out/.../bin/node_modules`
    # location. Calling `.resolve()` on `root / subpath` would follow those
    # leaf symlinks and produce a path outside the runfiles tree even when
    # the subpath itself is well-formed.
    assert_subpath_does_not_escape(
        package_name, subpath, f"Package {package_name} subpath escapes package root: {subpath}"
    )
    file_path = resolved_package_root / subpath
    if not file_path.exists():
        raise RuntimeError(f"Package file not found for {package_name}: {subpath} -> {file_path}")
    return file_path


def assert_subpath_does_not_escape(package_name: str, subpath: str, message: str) -> None:
    """Reject subpaths that contain absolute components or `..` escapes.

    Compares the lexically-normalized form of `subpath` against itself: if
    normalization changes anything to start with `..` or to become absolute,
    the input was trying to climb above the package root. This is a
    pre-symlink-resolution check, so it works correctly inside sandboxed
    Bazel runfiles trees where intermediate path components may be symlinks
    that point back into `bazel-out` and would confuse `Path.resolve()`.
    """
    if not isinstance(subpath, str):
        raise RuntimeError(f"{message}: non-string subpath {subpath!r}")
    if subpath.startswith(("/", "\\")):
        raise RuntimeError(f"{message}: subpath is absolute")
    # Normalize forward slashes; allow either separator on the way in.
    normalized = posixpath.normpath(subpath.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../"):
        raise RuntimeError(f"{message}: subpath climbs above package root")
    # `os.path.normpath` collapses redundant separators but does not catch
    # cases where the original subpath was an absolute path on Windows; the
    # absolute-check above plus this pure-forward-slash normalization is
    # sufficient on POSIX, which is the only target for this code path.


def package_path_segments(package_name: str) -> list[str]:
    if not isinstance(package_name, str) or package_name == "":
        raise RuntimeError(f"Invalid package name: {package_name}")
    segments = package_name.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise RuntimeError(f"Invalid package name: {package_name}")
    return segments
