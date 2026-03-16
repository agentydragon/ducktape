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
        """Parse a label string.

        Handles ``//pkg:target``, ``//pkg`` (short for ``//pkg:pkg``),
        ``@repo//pkg:target`` and ``@@repo//pkg:target``.
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
            # Support short form: //foo/bar implies //foo/bar:bar
            package = Path(rest)
            if not package.name:
                raise ValueError(f"Invalid Bazel label (no {_TARGET_SEP!r} separator and empty package): {s!r}")
            return cls(repo=repo, package=package, name=package.name)

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
        """Reconstruct the label string, using short form when possible.

        Short form omits the target when it matches the last package component:
        ``//foo/bar:bar`` → ``//foo/bar``.
        """
        # Path("") stringifies as "." in Python, but root package must be "" in a label.
        pkg = "" if self.package == Path() else str(self.package)
        repo_prefix = f"@{self.repo}//" if self.repo else "//"
        if self.name == self.package.name:
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


def run_query(
    expr: str, *, cwd: Path | None = None, persist_dir: Path | None = None, keep_going: bool = False
) -> list[BazelLabel]:
    """Run a ``bazel query`` and return the parsed labels.

    ``--output=label`` ensures every output line is a parseable label.
    The expression is passed via ``--query_file`` to avoid
    ``E2BIG`` / "Argument list too long" errors on large queries.

    Args:
        expr:        Bazel query expression.
        cwd:         Working directory for the subprocess.  ``None`` means
                     inherit the current working directory.
        persist_dir: When set, save ``query``, ``stdout``, ``stderr`` and
                     ``exit_code`` files there for CI artifact capture.
                     The directory must already exist; no subdir is created.
        keep_going:  Pass ``--keep_going`` and accept exit code 3 (partial
                     results due to errors in transitive closure).

    Raises :class:`subprocess.CalledProcessError` if the query exits non-zero
    (or non-3 when *keep_going*), with ``.stderr`` containing the captured
    error text.
    """
    cmd = ["bazel", "query", "--output=label"]
    if keep_going:
        cmd.append("--keep_going")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".bazelquery") as query_file:
        query_file.write(expr)
        query_file.flush()
        if persist_dir is not None:
            (persist_dir / "query").write_text(expr)
        cmd.append(f"--query_file={query_file.name}")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
    if persist_dir is not None:
        (persist_dir / "stdout").write_text(result.stdout)
        (persist_dir / "stderr").write_text(result.stderr)
        (persist_dir / "exit_code").write_text(str(result.returncode))
    ok_codes = {0, 3} if keep_going else {0}
    if result.returncode not in ok_codes:
        raise subprocess.CalledProcessError(result.returncode, "bazel", result.stdout, result.stderr)
    return [BazelLabel.parse(line) for line in result.stdout.splitlines() if line]
