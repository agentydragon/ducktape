"""Utilities for working with Bazel labels and query output."""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class BazelLabel:
    """A structured Bazel label with explicit repo, package and name fields.

    Not a ``str`` subtype — a plain string cannot flow in where a
    ``BazelLabel`` is expected, and a ``BazelLabel`` cannot be used as a
    ``str`` without an explicit conversion.

    Construct from a raw query-output string with :meth:`parse`.

    Attributes:
        repo:    Canonical repo name (without leading ``@``); empty string
                 for the main repository.
        package: Package directory path (e.g. ``Path("foo/bar")``);
                 ``Path(".")`` for the root package.
        name:    Bazel target name within the package — an opaque identifier
                 that for source files happens to be a relative path
                 (e.g. ``"sub/qux.go"``), but for rule targets is just a
                 plain name (e.g. ``"my_library"``).
    """

    repo: str
    package: Path
    name: str

    @classmethod
    def parse(cls, s: str) -> BazelLabel | None:
        """Parse a label string as produced by ``bazel query``.

        Handles ``//pkg:target``, ``@repo//pkg:target`` and the Bzlmod
        canonical ``@@repo//pkg:target`` forms.  Returns ``None`` for bare
        package references (no ``:``) and unrecognised formats.
        """
        if s.startswith("@@"):
            without_sigils = s.removeprefix("@@")
            if "//" not in without_sigils:
                return None
            repo, rest = without_sigils.split("//", 1)
        elif s.startswith("@"):
            without_sigil = s.removeprefix("@")
            if "//" not in without_sigil:
                return None
            repo, rest = without_sigil.split("//", 1)
        elif s.startswith("//"):
            repo, rest = "", s.removeprefix("//")
        else:
            return None

        if ":" not in rest:
            return None

        package, name = rest.split(":", 1)
        return cls(repo=repo, package=Path(package), name=Path(name))

    @property
    def is_external(self) -> bool:
        """True for labels that live in an external repository."""
        return bool(self.repo)


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
    return [label for line in result.stdout.splitlines() if line and (label := BazelLabel.parse(line)) is not None]


def label_to_path(label: BazelLabel) -> Path | None:
    """Return the workspace-relative path for a local source file label.

        BazelLabel(repo="", package=Path("foo/bar"), name=Path("baz.py"))  -> Path("foo/bar/baz.py")
        BazelLabel(repo="", package=Path("."),        name=Path("root.txt")) -> Path("root.txt")

    Returns ``None`` for external-repo labels.
    """
    if label.is_external:
        return None
    return label.package / label.name


def label_to_package(label: BazelLabel) -> Path | None:
    """Return the package directory for a local label.

        BazelLabel(repo="", package=Path("cluster/charts/attic"), name=...) -> Path("cluster/charts/attic")
        BazelLabel(repo="", package=Path("."),                    name=...) -> Path(".")

    Returns ``None`` for external-repo labels.
    """
    if label.is_external:
        return None
    return label.package
