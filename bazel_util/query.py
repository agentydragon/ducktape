"""Utilities for working with Bazel labels and query output."""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
from pathlib import Path

# Label format constants
_CANONICAL_SIGIL = "@@"
_REPO_SIGIL = "@"
_PKG_SEP = "//"
_TARGET_SEP = ":"


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
            raise ValueError(f"Invalid Bazel label (no {_TARGET_SEP!r} separator): {s!r}")

        package, name = rest.split(_TARGET_SEP, 1)
        return cls(repo=repo, package=Path(package), name=name)

    @classmethod
    def try_parse(cls, s: str) -> BazelLabel | None:
        """Like :meth:`parse` but returns ``None`` instead of raising."""
        try:
            return cls.parse(s)
        except ValueError:
            return None

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


def run_query(expr: str, *, cwd: Path) -> list[BazelLabel]:
    """Run a ``bazel query`` and return the parsed labels.

    The expression is passed via ``--query_file`` to avoid
    ``E2BIG`` / "Argument list too long" errors on large queries.

    Raises :class:`subprocess.CalledProcessError` if the query exits non-zero,
    with ``.stderr`` containing the captured error text.  Output lines that
    cannot be parsed as labels are silently skipped.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".bazelquery") as query_file:
        query_file.write(expr)
        query_file.flush()
        result = subprocess.run(
            ["bazel", "query", f"--query_file={query_file.name}"], capture_output=True, text=True, cwd=cwd, check=True
        )
    return [label for line in result.stdout.splitlines() if line and (label := BazelLabel.try_parse(line)) is not None]


def label_to_path(label: BazelLabel) -> Path | None:
    """Deprecated: use ``label.path`` instead."""
    return label.path


def label_to_package(label: BazelLabel) -> Path | None:
    """Deprecated: use ``label.package_path`` instead."""
    return label.package_path
