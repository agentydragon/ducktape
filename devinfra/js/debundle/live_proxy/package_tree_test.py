"""Tests for ``devinfra.js.debundle.live_proxy.package_tree``.

Regression coverage for the Bazel-runfiles symlink shape that
``aspect_rules_js`` produces: each package directory is materialized in the
runfiles tree as a real directory whose *leaf files* are symlinks back to the
original ``bazel-out/.../bin/node_modules/...`` package. ``Path.resolve()`` on
a joined ``root / subpath`` therefore follows the leaf symlinks and returns a
path that does **not** start with ``root.resolve()`` — even when ``subpath``
is a perfectly well-formed relative path inside the package. The old
escape-detection logic, which compared resolved paths, treated this as an
escape and refused to serve mermaid (and other vendored) packages.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.js.debundle.live_proxy.package_tree import resolve_package_subpath


class PackageSubpathSymlinkLeafTest(unittest.TestCase):
    """Reproduce the Bazel runfiles shape and assert subpath resolution works."""

    def test_resolves_subpath_when_leaf_file_is_symlink_outside_root(self) -> None:
        # Layout:
        #   <tmp>/bin_node_modules/mermaid/dist/chunks/x.mjs   <-- real file
        #   <tmp>/runfiles/.../mermaid/                        <-- real directory
        #   <tmp>/runfiles/.../mermaid/dist/                   <-- real directory
        #   <tmp>/runfiles/.../mermaid/dist/chunks/            <-- real directory
        #   <tmp>/runfiles/.../mermaid/dist/chunks/x.mjs       <-- symlink ->
        #                                  <tmp>/bin_node_modules/mermaid/dist/chunks/x.mjs
        # This is exactly how Bazel's processwrapper-sandbox materializes
        # `node_modules/<pkg>` for `aspect_rules_js` packages: parent dirs
        # are real directories, leaves are symlinks back into bin.
        tmp = Path(tempfile.mkdtemp(prefix="pkg-tree-symlink-test-"))
        bin_dir = tmp / "bin_node_modules" / "mermaid" / "dist" / "chunks"
        bin_dir.mkdir(parents=True)
        real_file = bin_dir / "architectureDiagram.mjs"
        real_file.write_text("export const x = 1;\n", encoding="utf-8")

        runfiles_pkg = tmp / "runfiles" / "_main" / "node_modules" / "mermaid"
        (runfiles_pkg / "dist" / "chunks").mkdir(parents=True)
        symlink = runfiles_pkg / "dist" / "chunks" / "architectureDiagram.mjs"
        symlink.symlink_to(real_file)

        # Bazel hands the live-proxy the runfiles directory as the package
        # root. The subpath request mirrors what the vendor manifest stores.
        result = resolve_package_subpath("mermaid", "dist/chunks/architectureDiagram.mjs", package_root=runfiles_pkg)
        # The returned path must stay inside the runfiles tree (i.e. inside
        # the package root the caller gave us). The old implementation
        # ``.resolve()``d through the leaf symlink and returned a path
        # outside runfiles, which broke downstream URL construction.
        assert result == runfiles_pkg / "dist" / "chunks" / "architectureDiagram.mjs"
        # And the path must of course be readable.
        assert result.read_text(encoding="utf-8") == "export const x = 1;\n"

    def test_resolves_subpath_when_package_root_is_symlinked_outside_sandbox(self) -> None:
        # Outside-sandbox shape: the package root itself is a symlink to
        # bin. This used to work and must keep working.
        tmp = Path(tempfile.mkdtemp(prefix="pkg-tree-symlinked-root-test-"))
        real_pkg = tmp / "bin_node_modules" / "mermaid"
        (real_pkg / "dist" / "chunks").mkdir(parents=True)
        real_file = real_pkg / "dist" / "chunks" / "x.mjs"
        real_file.write_text("export const y = 2;\n", encoding="utf-8")

        runfiles_parent = tmp / "runfiles" / "_main" / "node_modules"
        runfiles_parent.mkdir(parents=True)
        runfiles_pkg = runfiles_parent / "mermaid"
        runfiles_pkg.symlink_to(real_pkg)

        result = resolve_package_subpath("mermaid", "dist/chunks/x.mjs", package_root=runfiles_pkg)
        assert result.read_text(encoding="utf-8") == "export const y = 2;\n"


class SubpathEscapeTest(unittest.TestCase):
    """The escape check must still reject `..` and absolute paths in subpaths.

    Exercised via the public ``resolve_package_subpath`` API so the test does
    not depend on the implementation choosing one helper name over another.
    """

    def _pkg(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="pkg-tree-escape-test-"))
        (tmp / "pkg").mkdir()
        return tmp / "pkg"

    def test_rejects_parent_dir_escape(self) -> None:
        with pytest.raises(RuntimeError, match=r"escapes? package root"):
            resolve_package_subpath("p", "../etc/passwd", package_root=self._pkg())

    def test_rejects_buried_parent_dir_escape(self) -> None:
        # `a/../../etc/passwd` normalizes to `../etc/passwd`.
        with pytest.raises(RuntimeError, match=r"escapes? package root"):
            resolve_package_subpath("p", "a/../../etc/passwd", package_root=self._pkg())

    def test_accepts_clean_subpaths(self) -> None:
        pkg = self._pkg()
        (pkg / "dist").mkdir()
        (pkg / "dist" / "b.mjs").write_text("export const b = 1;\n", encoding="utf-8")
        result = resolve_package_subpath("p", "dist/b.mjs", package_root=pkg)
        assert result.read_text(encoding="utf-8") == "export const b = 1;\n"


if __name__ == "__main__":
    pytest_bazel.main()
