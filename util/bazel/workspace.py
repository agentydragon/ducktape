"""Bazel workspace utilities: labels, workspace operations, and environment variables.

``bazel run`` sets two env vars that let tools find the source tree:

- ``BUILD_WORKSPACE_DIRECTORY`` — the Bazel workspace root (repo root)
- ``BUILD_WORKING_DIRECTORY`` — the cwd where ``bazel run`` was invoked

Both fall back to ``Path.cwd()`` when not running under ``bazel run``.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
from pathlib import Path

# Label format constants
_CANONICAL_SIGIL = "@@"
_REPO_SIGIL = "@"
_PKG_SEP = "//"
_TARGET_SEP = ":"


def get_build_workspace_directory() -> Path:
    """Bazel workspace root (repo root). Falls back to cwd outside ``bazel run``."""
    if workspace := os.environ.get("BUILD_WORKSPACE_DIRECTORY"):
        return Path(workspace)
    return Path.cwd()


def get_build_working_directory() -> Path:
    """Directory where ``bazel run`` was invoked. Falls back to cwd outside ``bazel run``."""
    if build_wd := os.environ.get("BUILD_WORKING_DIRECTORY"):
        return Path(build_wd)
    return Path.cwd()


@dataclasses.dataclass(frozen=True)
class BazelLabel:
    """A structured Bazel label with explicit repo, package and name fields.

    Not a ``str`` subtype — a plain string cannot flow in where a
    ``BazelLabel`` is expected, and a ``BazelLabel`` cannot be used as a
    ``str`` without an explicit conversion.

    Construct from a raw query-output string with :meth:`parse` (raises on
    bad format) or :meth:`try_parse` (returns ``None`` on bad format).

    Attributes:
        repo:    Canonical repo name (without leading ``@``); empty string
                 for the main repository.
        package: Package directory path (e.g. ``Path("foo/bar")``);
                 ``Path("")`` for the root package.
        name:    Bazel target name within the package — an opaque identifier
                 that for source files happens to be a relative path
                 (e.g. ``"sub/qux.go"``), but for rule targets is just a
                 plain name (e.g. ``"my_library"``).
    """

    repo: str
    package: Path
    name: str

    @classmethod
    def parse(cls, s: str) -> BazelLabel:
        """Parse a label string as produced by ``bazel query``.

        Handles ``//pkg:target``, ``@repo//pkg:target`` and the Bzlmod
        canonical ``@@repo//pkg:target`` forms.

        Raises :class:`ValueError` for bare package references (no ``:``)
        and unrecognised formats.  Use :meth:`try_parse` if ``None`` is
        preferred over an exception.
        """
        if s.startswith(_CANONICAL_SIGIL):
            rest_after_sigil = s.removeprefix(_CANONICAL_SIGIL)
            if _PKG_SEP not in rest_after_sigil:
                raise ValueError(f"Invalid Bazel label (no {_PKG_SEP!r} after {_CANONICAL_SIGIL!r}): {s!r}")
            repo, rest = rest_after_sigil.split(_PKG_SEP, 1)
        elif s.startswith(_REPO_SIGIL):
            rest_after_sigil = s.removeprefix(_REPO_SIGIL)
            if _PKG_SEP not in rest_after_sigil:
                raise ValueError(f"Invalid Bazel label (no {_PKG_SEP!r} after {_REPO_SIGIL!r}): {s!r}")
            repo, rest = rest_after_sigil.split(_PKG_SEP, 1)
        elif s.startswith(_PKG_SEP):
            repo, rest = "", s.removeprefix(_PKG_SEP)
        else:
            raise ValueError(f"Invalid Bazel label (must start with {_PKG_SEP!r} or {_REPO_SIGIL!r}): {s!r}")

        if _TARGET_SEP not in rest:
            # Short form: //pkg means //pkg:pkg (name = last component)
            package = rest
            if not package:
                raise ValueError(f"Invalid Bazel label (no {_TARGET_SEP!r} separator and empty package): {s!r}")
            name = Path(package).name
            return cls(repo=repo, package=Path(package), name=name)

        package, name = rest.split(_TARGET_SEP, 1)
        return cls(repo=repo, package=Path(package), name=name)

    @classmethod
    def try_parse(cls, s: str) -> BazelLabel | None:
        """Like :meth:`parse` but returns ``None`` instead of raising."""
        try:
            return cls.parse(s)
        except ValueError:
            return None

    def __str__(self) -> str:
        """Reconstruct the label string, using short form when name matches last package component."""
        pkg = "" if self.package == Path() else str(self.package)
        repo_prefix = f"@{self.repo}//" if self.repo else "//"
        # Short form: omit :name when it matches the last package component
        if pkg and self.name == self.package.name:
            return f"{repo_prefix}{pkg}"
        return f"{repo_prefix}{pkg}:{self.name}"

    @property
    def is_external(self) -> bool:
        """True for labels that live in an external repository."""
        return bool(self.repo)

    @property
    def path(self) -> Path | None:
        """Workspace-relative path for local source file labels.

        Returns ``package / name`` for local labels, ``None`` for external.

        TODO: Ideally we'd verify that this label actually refers to a source
        file (``kind('source file', ...)``) before treating ``name`` as a path,
        since rule-target names are opaque identifiers, not file paths.
        """
        if self.is_external:
            return None
        return self.package / self.name

    @property
    def package_path(self) -> Path | None:
        """Package directory for local labels; ``None`` for external.

        BazelLabel(repo="", package=Path("cluster/charts/attic"), ...) -> Path("cluster/charts/attic")
        BazelLabel(repo="", package=Path(""),                     ...) -> Path("")
        """
        if self.is_external:
            return None
        return self.package


@dataclasses.dataclass(frozen=True)
class BazelWorkspace:
    """A local Bazel workspace rooted at a specific directory."""

    root: Path

    def find_package(self, filepath: Path) -> Path | None:
        """Find the Bazel package containing a file by walking up to find BUILD."""
        current = self.root / filepath.parent
        while current >= self.root:
            if (current / "BUILD.bazel").exists() or (current / "BUILD").exists():
                return current.relative_to(self.root)
            if current == self.root:
                break
            current = current.parent
        return None

    def file_to_label(self, filepath: Path) -> BazelLabel | None:
        """Convert a repo-relative filepath to a Bazel source file label."""
        pkg = self.find_package(filepath)
        if pkg is None:
            return None
        rel = filepath.relative_to(pkg) if pkg != Path() else filepath
        return BazelLabel(repo="", package=pkg, name=str(rel))

    def query(
        self,
        expr: str,
        *,
        persist_dir: Path | None = None,
        keep_going: bool = False,
        timeout: int | None = None,
        universe_scope: str | None = None,
    ) -> list[BazelLabel]:
        """Run ``bazel query`` in this workspace.

        ``--output=label`` ensures every output line is a parseable label.
        The expression is passed via ``--query_file`` to avoid
        ``E2BIG`` / "Argument list too long" errors on large queries.

        Raises :class:`subprocess.CalledProcessError` if the query exits non-zero
        (or non-3 when *keep_going*), with ``.stderr`` containing the captured
        error text.
        """
        cmd = ["bazel", "query", "--output=label"]
        if keep_going:
            cmd.append("--keep_going")
        if universe_scope is not None:
            cmd.append(f"--universe_scope={universe_scope}")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bazelquery") as query_file:
            query_file.write(expr)
            query_file.flush()
            if persist_dir is not None:
                (persist_dir / "query").write_text(expr)
            cmd.append(f"--query_file={query_file.name}")
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root, check=False, timeout=timeout)
        if persist_dir is not None:
            (persist_dir / "stdout").write_text(result.stdout)
            (persist_dir / "stderr").write_text(result.stderr)
            (persist_dir / "exit_code").write_text(str(result.returncode))
        ok_codes = {0, 3} if keep_going else {0}
        if result.returncode not in ok_codes:
            raise subprocess.CalledProcessError(result.returncode, "bazel", result.stdout, result.stderr)
        return [BazelLabel.parse(line) for line in result.stdout.splitlines() if line]

    def test(self, targets: list[str], *, check_up_to_date: bool = False, timeout: int | None = None) -> int:
        """Run ``bazel test`` and return the exit code."""
        cmd = ["bazel", "test"]
        if check_up_to_date:
            cmd.append("--check_tests_up_to_date")
        cmd.extend(targets)
        result = subprocess.run(cmd, check=False, cwd=self.root, timeout=timeout)
        return result.returncode

    def shutdown(self) -> None:
        """Shut down the Bazel server for this workspace."""
        subprocess.run(["bazel", "shutdown"], cwd=self.root, check=True, capture_output=True)
