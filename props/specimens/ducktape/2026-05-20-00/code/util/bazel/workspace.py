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
import tempfile
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


def get_bazel_bin() -> Path:
    """Return the absolute path to bazel-bin via ``bazel info``."""
    return Path(subprocess.check_output(["bazel", "info", "bazel-bin"], text=True).strip())


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

    Raises ``RuntimeError`` if neither ``bbr`` nor ``bazelisk`` is on PATH.
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
    output_base: Path | None = None
    startup_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.backend != BazelBackend.LOCAL:
            if self.output_base is not None:
                raise ValueError(f"output_base is not supported with {self.backend}")
            if self.startup_flags:
                raise ValueError(f"startup_flags is not supported with {self.backend}")

    @property
    def _bazel_prefix(self) -> tuple[str, ...]:
        """Base bazel command with optional --output_base and startup flags."""
        cmd = list(self.backend.command)
        if self.output_base is not None:
            cmd.append(f"--output_base={self.output_base}")
        cmd.extend(self.startup_flags)
        return tuple(cmd)

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
        profile_path: Path | None = None,
    ) -> list[BazelLabel]:
        """Run ``bazel query`` and return parsed labels.

        Short queries are passed as a positional arg (works with both local
        bazel and ``bbr``).  Queries exceeding 100 KB fall back to
        ``--query_file`` (local only — ``bbr`` cannot access local temp
        files).
        """
        cmd = [*self._bazel_prefix, "query", "--output=label"]
        if keep_going:
            cmd.append("--keep_going")
        if universe_scope is not None:
            cmd.append(f"--universe_scope={universe_scope}")
        if profile_path is not None:
            cmd.extend([f"--profile={profile_path}", "--generate_json_trace_profile"])
        if persist_dir is not None:
            (persist_dir / "query").write_text(expr)
        # Prefer inline query arg (works with both local bazel and bbr).
        # Fall back to --query_file for large queries (local only — bbr
        # can't access local temp files on the runner).
        max_inline_bytes = 100_000  # conservative; Linux MAX_ARG_STRLEN is 128 KiB
        query_file_path: Path | None = None
        if len(expr.encode()) <= max_inline_bytes:
            cmd.append(expr)
        elif self.backend == BazelBackend.LOCAL:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".bazelquery", delete=False) as f:
                f.write(expr)
            query_file_path = Path(f.name)
            cmd.append(f"--query_file={query_file_path}")
        else:
            raise RuntimeError(
                f"Query too large ({len(expr)} chars) for command line,"
                f" and --query_file is not supported with {self.backend}"
            )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root, check=False, timeout=timeout)
        finally:
            if query_file_path is not None:
                query_file_path.unlink(missing_ok=True)
        if persist_dir is not None:
            (persist_dir / "stdout").write_text(result.stdout)
            (persist_dir / "stderr").write_text(result.stderr)
            (persist_dir / "exit_code").write_text(str(result.returncode))
        ok_codes = {0, 3} if keep_going else {0}
        if result.returncode not in ok_codes:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

        # TODO: bbr mixes its own log lines (git sync, progress) into
        # stdout.  We filter to lines that look like Bazel labels.  A cleaner
        # approach would be to use `bb execute --input_root . --output stdio`
        # which gives clean separated stdout/stderr, but it uploads the whole
        # repo each time and doesn't benefit from bbr's runner recycling.
        def _is_label(line: str) -> bool:
            return line.startswith((_PKG_SEP, _REPO_SIGIL))

        return [BazelLabel.parse(line) for line in result.stdout.splitlines() if _is_label(line)]

    def test(self, targets: list[str], *, check_up_to_date: bool = False, timeout: int | None = None) -> int:
        """Run ``bazel test`` and return the exit code."""
        cmd = [*self._bazel_prefix, "test"]
        if check_up_to_date:
            cmd.append("--check_tests_up_to_date")
        cmd.extend(targets)
        result = subprocess.run(cmd, check=False, cwd=self.root, timeout=timeout)
        return result.returncode

    def shutdown(self) -> None:
        """Shut down the Bazel server for this workspace."""
        subprocess.run([*self._bazel_prefix, "shutdown"], cwd=self.root, check=False, capture_output=True)
