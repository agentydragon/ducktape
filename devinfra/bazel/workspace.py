"""Bazel workspace utilities: labels, workspace operations, and environment variables.

``bazel run`` sets two env vars that let tools find the source tree:

- ``BUILD_WORKSPACE_DIRECTORY`` — the Bazel workspace root (repo root)
- ``BUILD_WORKING_DIRECTORY`` — the cwd where ``bazel run`` was invoked

Both fall back to ``Path.cwd()`` when not running under ``bazel run``.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

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

        Also accepts short package forms such as ``//pkg``, where the target
        name defaults to the final package component. Raises :class:`ValueError`
        for unrecognised formats or empty short-form packages. Use
        :meth:`try_parse` if ``None`` is preferred over an exception.
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


class BazelBackend(enum.Enum):
    """How Bazel commands are executed."""

    LOCAL = "local"
    """Local ``bazelisk`` invocation.

    ``bazelisk`` is canonical over bare ``bazel``: it reads ``.bazelversion``
    to download and pin the exact Bazel version, so every developer and CI run
    uses the same binary.
    """

    BUILDBUDDY = "buildbuddy"
    """Remote execution via ``bbr``."""

    @property
    def command(self) -> tuple[str, ...]:
        match self:
            case BazelBackend.LOCAL:
                return ("bazelisk",)
            case BazelBackend.BUILDBUDDY:
                return ("bbr",)


def detect_bazel_backend() -> BazelBackend:
    """Return BUILDBUDDY when ``bbr`` + ``BUILDBUDDY_API_KEY`` are available, else LOCAL.

    LOCAL uses ``bazelisk`` (not bare ``bazel``) because bazelisk reads
    ``.bazelversion`` to pin the exact Bazel release, while a system ``bazel``
    binary may be any version.

    Raises ``FileNotFoundError`` if no usable backend is available.
    """
    if os.environ.get("BUILDBUDDY_API_KEY") and shutil.which("bbr"):
        return BazelBackend.BUILDBUDDY
    if not shutil.which("bazelisk"):
        raise FileNotFoundError(
            "Neither bbr nor bazelisk found on PATH. "
            "Install bazelisk (e.g. nix develop) or set BUILDBUDDY_API_KEY and install bbr."
        )
    if not shutil.which("bbr"):
        logger.warning("bbr not on PATH, falling back to local bazelisk")
    elif not os.environ.get("BUILDBUDDY_API_KEY"):
        logger.warning("BUILDBUDDY_API_KEY not set, falling back to local bazelisk")
    return BazelBackend.LOCAL


@dataclasses.dataclass(frozen=True)
class BazelWorkspace:
    """A local Bazel workspace rooted at a specific directory."""

    root: Path
    backend: BazelBackend

    def query(self, expr: str, *, keep_going: bool = False) -> list[BazelLabel]:
        """Run ``bazel query`` and return parsed labels.

        ``expr`` is one command-line argument (works with both local bazel and
        ``bbr``), so it must fit the 128 KiB Linux per-argument limit.
        """
        cmd = [*self.backend.command, "query", "--output=label"]
        if keep_going:
            cmd.append("--keep_going")
        cmd.append(expr)
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root, check=False)
        ok_codes = {0, 3} if keep_going else {0}
        if result.returncode not in ok_codes:
            error = subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            # CalledProcessError's message omits stderr, where Bazel says why the query failed.
            error.add_note(result.stderr)
            raise error

        # TODO: bbr mixes its own log lines (git sync, progress) into
        # stdout.  We filter to lines that look like Bazel labels.  A cleaner
        # approach would be to use `bb execute --input_root . --output stdio`
        # which gives clean separated stdout/stderr, but it uploads the whole
        # repo each time and doesn't benefit from bbr's runner recycling.
        labels = (BazelLabel.try_parse(line) for line in result.stdout.splitlines())
        return [label for label in labels if label is not None]
