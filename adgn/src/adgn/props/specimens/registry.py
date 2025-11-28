from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
import json
import logging
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlunparse
from urllib.request import urlopen
import uuid
import warnings

import _jsonnet
import pygit2
from filelock import FileLock
from platformdirs import user_cache_dir
from pydantic import BaseModel, ConfigDict
import yaml

from ..ids import FalsePositiveID, TruePositiveID
from ..models.issue import IssueCore, Occurrence, SpecimenIssuesLoadError
from ..models.specimen import GitHubSource, GitSource, LocalSource, SpecimenDoc
from ..paths import FileType, classify_path
from ..prop_utils import pkg_dir
from ..rationale import Rationale
from ..validation_context import SpecimenContext

logger = logging.getLogger(__name__)


def _make_unique_temp_path(parent: Path, suffix: str = ".tar.gz") -> Path:
    """Create a unique temporary file path to avoid conflicts in parallel execution."""
    return parent / f".tmp-{uuid.uuid4().hex}{suffix}"


def _specimen_extract_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """Custom tarfile extraction filter for specimens.

    Based on tarfile.data_filter but skips absolute symlinks instead of raising error.
    Specimens are read-only training data from known commits, so absolute symlinks
    (while discouraged) don't pose a security risk here.
    """
    # Use data_filter as base, but catch AbsoluteLinkError
    try:
        return tarfile.data_filter(member, path)
    except tarfile.AbsoluteLinkError:
        # Skip absolute symlinks with warning
        logger.warning(
            f"Skipping absolute symlink in specimen: {member.name} -> {member.linkname}"
        )
        return None


# TODO: Consider generic Issue[IDType] to reduce duplication between these models
class CanonicalIssue(BaseModel):
    """Canonical true positive issue with typed namespaced ID."""

    id: TruePositiveID
    rationale: Rationale
    occurrences: list[Occurrence]

    model_config = ConfigDict(frozen=True)


class KnownFalsePositive(BaseModel):
    """Known false positive issue with typed namespaced ID."""

    id: FalsePositiveID
    rationale: Rationale
    occurrences: list[Occurrence]

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class IssueRecord:
    core: IssueCore
    instances: list[Occurrence]


@dataclass(frozen=True)
class IssuesLoadResult:
    items: list[IssueRecord]
    errors: list[str]


# ---- Shared Jsonnet loader helpers ----
JSONNET_LIBDIR = Path(__file__).resolve().parent


def _jsonnet_importer(base: str, rel: str) -> tuple[str, bytes]:
    cand1 = (Path(base) / rel).resolve()
    if cand1.is_file():
        return str(cand1), cand1.read_bytes()
    rel_name = Path(rel).name
    cand2 = (JSONNET_LIBDIR / rel_name).resolve()
    if cand2.is_file():
        return str(cand2), cand2.read_bytes()
    raise RuntimeError(f"import not found: base={base!r} rel={rel!r}")


def _jsonnet_evaluate_to_dict(spec_dir: Path, subdir: str, should_flag: bool) -> dict[str, dict] | None:
    """Evaluate Jsonnet files to raw dicts without Pydantic validation.

    Returns:
        Dict mapping issue_id -> raw dict (with id, should_flag injected), or None if directory missing.
    """
    dir_path = spec_dir / subdir
    if not dir_path.is_dir():
        return None

    # Discover all libsonnet files
    issue_files = sorted(dir_path.glob("*.libsonnet"))
    if not issue_files:
        return {}

    # Optimization: batch-load all issues in single Jsonnet evaluation
    # Jsonnet requires static import paths, so we compose the aggregator in Python
    # Each import is merged with {id, should_flag} to produce complete issue objects
    # Use absolute paths since evaluate_snippet has no base file for relative imports
    imports = []
    for p in issue_files:
        name = p.stem
        abs_path = str(p.resolve())
        imports.append(f"  {json.dumps(name)}: (import {json.dumps(abs_path)}) + {{id: {json.dumps(name)}, should_flag: {json.dumps(should_flag)}}}")

    snippet = "{\n" + ",\n".join(imports) + "\n}"

    eval_snippet = cast(Callable[..., Any], _jsonnet.evaluate_snippet)
    raw_obj = eval_snippet(
        f"<batch:{subdir}>",
        snippet,
        jpathdir=[str(JSONNET_LIBDIR)],
        import_callback=_jsonnet_importer,
    )
    if not isinstance(raw_obj, str):
        raise SpecimenIssuesLoadError([f"{subdir}: Jsonnet returned non-string"])

    all_issues = json.loads(raw_obj)
    if not isinstance(all_issues, dict):
        raise SpecimenIssuesLoadError([f"{subdir}: Expected dict, got {type(all_issues)}"])

    return all_issues


def _validate_issues_from_dicts(
    raw_issues: dict[str, dict],
    validation_context: dict,
    strict: bool,
) -> IssuesLoadResult:
    """Validate pre-evaluated issue dicts with complete context.

    Args:
        raw_issues: Dict mapping issue_id -> raw dict (from Jsonnet evaluation)
        validation_context: Complete validation context (specimen_context with files + IDs)
        strict: If True, raise on any validation errors

    Returns:
        IssuesLoadResult with validated items and errors
    """
    items: list[IssueRecord] = []
    errors: list[str] = []

    for issue_id, issue_dict in raw_issues.items():
        if not isinstance(issue_dict, dict):
            errors.append(f"{issue_id}: Not a dict (got {type(issue_dict)})")
            continue

        try:
            # IssueCore validation (copy dict, drop instances field)
            core_fields = {k: v for k, v in issue_dict.items() if k != "instances"}
            core = IssueCore.model_validate(core_fields, context=validation_context)
            inst_raw = issue_dict.get("instances", [])
            instances = [Occurrence.model_validate(inst, context=validation_context) for inst in inst_raw]
            items.append(IssueRecord(core=core, instances=instances))
        except Exception as e:
            errors.append(f"{issue_id}: {e}")
            continue

    if errors and strict:
        raise SpecimenIssuesLoadError(errors)
    return IssuesLoadResult(items=items, errors=errors)




def _xdg_cache_base() -> Path:
    # Prefer shared cache dir alongside existing helpers

    root = Path(user_cache_dir(appname="adgn-llm", appauthor=False)) / "specimens"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_tar_gz_to_temp(archive: Path) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="adgn-specimen-extract-"))
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(tmpdir, filter=_specimen_extract_filter)
    for p in tmpdir.iterdir():
        if p.is_dir():
            return p.resolve()
    return tmpdir


def _repack_dir_with_mtime(src_dir: Path, out_archive: Path, mtime: int = 0) -> None:
    out_archive.parent.mkdir(parents=True, exist_ok=True)

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Exclude VCS internals from archives to avoid permission issues and reduce size
        # Skip any member whose path includes a '.git' segment
        parts = ti.name.split("/")
        if ".git" in parts:
            return None
        ti.mtime = int(mtime)
        # Preserve uid/gid; determinism here only requires pinned mtime
        return ti

    tmp = _make_unique_temp_path(out_archive.parent)
    logger.debug("repacking %s -> %s (via %s, filter .git, mtime=%s)", src_dir, out_archive, tmp.name, mtime)

    try:
        with tarfile.open(tmp, "w:gz", format=tarfile.PAX_FORMAT) as tf:
            tf.add(src_dir, arcname=Path(src_dir).name, filter=_filter)
        logger.debug("repack complete, renaming %s -> %s", tmp.name, out_archive.name)
        tmp.replace(out_archive)
    except Exception:
        logger.debug("repack failed, cleaning up %s", tmp.name)
        if tmp.exists():
            tmp.unlink()
        raise


def _repack_tar_with_mtime(archive: Path, mtime: int = 0) -> Path:
    extracted = _extract_tar_gz_to_temp(archive)
    _repack_dir_with_mtime(extracted, archive, mtime=mtime)
    shutil.rmtree(extracted, ignore_errors=True)
    return archive


def _default_gitconfig() -> Path | None:
    cfg = pkg_dir() / "gitconfig.local"
    return cfg if cfg.exists() else None


def _download_github_to(owner: str, repo: str, ref: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = urlunparse(("https", "codeload.github.com", f"/{owner}/{repo}/tar.gz/{ref}", "", "", ""))
    tmp = _make_unique_temp_path(dest.parent)
    logger.debug("downloading %s -> %s (via %s)", url, dest, tmp.name)

    try:
        with urlopen(url) as resp:
            tmp.write_bytes(resp.read())
        logger.debug("download complete, renaming %s -> %s", tmp.name, dest.name)
        tmp.replace(dest)
        return True
    except (URLError, HTTPError) as e:
        logger.debug("download failed (%s), cleaning up %s", e, tmp.name)
        if tmp.exists():
            tmp.unlink()
        return False


def _checkout_detached(repo: pygit2.Repository, ref: str) -> None:
    """Checkout a ref in detached HEAD mode using pygit2."""
    # Resolve the ref to a commit
    commit = repo.revparse_single(ref).peel(pygit2.Commit)
    # Checkout the tree
    repo.checkout_tree(commit.tree)
    # Set HEAD to the commit (detached)
    repo.set_head(commit.id)


def _clone_from_bundle_subprocess(bundle_path: str, tmpdir: Path, ref: str) -> None:
    """Clone from a git bundle file using subprocess.

    pygit2/libgit2 doesn't support cloning from bundle files directly,
    so we fall back to subprocess for this case.
    """
    import subprocess

    subprocess.run(
        ["git", "clone", bundle_path, str(tmpdir)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(tmpdir), "checkout", "--detach", ref],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _create_archive_from_git(
    url: str,
    ref: str,
    out_archive: Path,
    gitconfig: Path | None,  # noqa: ARG001 - kept for API compatibility, pygit2 uses system git config
) -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="adgn-specimen-git-"))

    try:
        # Bundle files require subprocess - pygit2 doesn't support bundle cloning
        if url.startswith("file://") and url.removeprefix("file://").endswith(".bundle"):
            _clone_from_bundle_subprocess(url.removeprefix("file://"), tmpdir, ref)
        else:
            # Regular repositories (file:// or network URLs) - use pygit2
            repo = pygit2.clone_repository(url, str(tmpdir))
            _checkout_detached(repo, ref)

        # Drop VCS internals to keep archives small and writable on extract
        shutil.rmtree(tmpdir / ".git", ignore_errors=True)
        _repack_dir_with_mtime(tmpdir, out_archive, mtime=0)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_archive_for_specimen_slug(
    man: SpecimenDoc,
    manifest_path: Path,
    gitconfig: Path | None,
) -> Path:
    """Ensure a cached archive exists for the specimen.

    The slug is computed from the manifest path as repo/name.
    For GitSource with commit SHA: ~/.cache/adgn-llm/specimens/{repo}/{name}-{commit}.tar.gz
    Otherwise: ~/.cache/adgn-llm/specimens/{repo}/{name}.tar.gz

    Uses a lock file to prevent concurrent cache creation from multiple processes.
    """
    gitconfig = gitconfig or _default_gitconfig()
    # Extract hierarchical slug from path: specimens/{repo}/{name}/manifest.yaml -> repo/name
    specimen_dir = manifest_path.parent
    repo_name = specimen_dir.parent.name
    specimen_name = specimen_dir.name
    slug = f"{repo_name}/{specimen_name}"

    # Include commit SHA in cache key for GitSource to avoid staleness
    cache_filename = specimen_name
    if isinstance(man.source, GitSource) and man.source.commit:
        cache_filename = f"{specimen_name}-{man.source.commit}"

    # Cache hierarchically
    out = _xdg_cache_base() / repo_name / f"{cache_filename}.tar.gz"
    lock_file = out.with_suffix(".lock")

    logger.debug("ensure_archive slug=%s out=%s", slug, out.name)

    # Fast path: if archive already exists, return it without acquiring lock
    if out.exists():
        logger.debug("archive exists (fast path), returning %s", out.name)
        return out

    logger.debug("archive missing, acquiring lock %s", lock_file.name)
    # Acquire lock to prevent concurrent cache creation
    with FileLock(lock_file):
        logger.debug("lock acquired, checking if archive was created while waiting")
        # Check again after acquiring lock (another process may have created it)
        if out.exists():
            logger.debug("archive exists (created while waiting), returning %s", out.name)
            return out

        logger.debug("archive still missing, creating it")

        if isinstance(man.source, GitHubSource):
            if _download_github_to(man.source.org, man.source.repo, man.source.ref, out):
                _repack_tar_with_mtime(out, mtime=0)
                return out
            if (
                _create_archive_from_git(
                    urlunparse(
                        (
                            "https",
                            "github.com",
                            f"/{man.source.org}/{man.source.repo}.git",
                            "",
                            "",
                            "",
                        )
                    ),
                    man.source.ref,
                    out,
                    gitconfig,
                )
                and out.exists()
            ):
                return out
        elif isinstance(man.source, GitSource):
            # Prefer commit SHA for exact fetching; ref is optional and may have moved
            git_ref = man.source.commit

            if man.source.url.startswith("https://github.com/"):
                parts = (
                    man.source.url.removeprefix("https://github.com/")
                    .rstrip("/")
                    .removesuffix(".git")
                    .split("/")
                )
                if len(parts) >= 2 and _download_github_to(
                    parts[0],
                    parts[1],
                    git_ref,
                    out,
                ):
                    _repack_tar_with_mtime(out, mtime=0)
                    return out
            # Resolve relative file:// URLs relative to the manifest directory
            url = resolve_bundle_url(manifest_path, man.source.url)

            if (
                _create_archive_from_git(url, git_ref, out, gitconfig)
                and out.exists()
            ):
                return out
        elif isinstance(man.source, LocalSource):
            src = (manifest_path.parent / man.source.root).resolve()
            _repack_dir_with_mtime(src, out, mtime=0)
            return out
        raise SystemExit(
            f"Can't archive specimen cache for '{slug}' (source={type(man.source).__name__}); ",
        )


def resolve_bundle_url(manifest_path: Path, source_url: str) -> str:
    """Resolve bundle URL, handling relative file:// paths.

    Args:
        manifest_path: Path to manifest.yaml file
        source_url: Source URL from manifest (may be relative file://)

    Returns:
        Absolute URL (file:// URLs are resolved relative to manifest directory)
    """
    url = source_url
    if url.startswith("file://"):
        file_path = url.removeprefix("file://")
        if not file_path.startswith("/"):
            resolved_path = (manifest_path.parent / file_path).resolve()
            url = f"file://{resolved_path}"
    return url


def resolve_source_root(
    man: SpecimenDoc,
    manifest_path: Path,
    gitconfig: Path | None,
) -> Path:
    gitconfig = gitconfig or _default_gitconfig()
    if isinstance(man.source, GitHubSource | GitSource):
        archive = ensure_archive_for_specimen_slug(man, manifest_path, gitconfig)
        return _extract_tar_gz_to_temp(archive)
    if isinstance(man.source, LocalSource):
        # Use existing local copy helper for consistency
        src = (manifest_path.parent / man.source.root).resolve()
        tmpdir = Path(tempfile.mkdtemp(prefix="adgn-specimen-local-"))
        dest = tmpdir / src.name
        shutil.copytree(src, dest)
        return dest
    raise SystemExit(f"Unsupported source type: {type(man.source)}")


def list_specimen_names(base: Path) -> list[str]:
    """List all specimen names in hierarchical format (repo/name).

    Specimens are organized as:
      specimens/
        {repo}/
          {name}/
            manifest.yaml

    Returns specimen IDs like "ducktape/2025-11-20-adgn", "crush/2025-08-30-internal_db"
    """
    names = []
    for repo_dir in base.iterdir():
        if not repo_dir.is_dir() or repo_dir.name.startswith(("_", ".")):
            continue
        for specimen_dir in repo_dir.iterdir():
            if specimen_dir.is_dir() and (specimen_dir / "manifest.yaml").exists():
                names.append(f"{repo_dir.name}/{specimen_dir.name}")
    return sorted(names)


def find_specimens_base() -> Path:
    """Resolve the installed specimens directory deterministically via package resources.

    This must exist inside the installed package; no filesystem fallbacks are used.
    """
    traversable = resources.files("adgn.props").joinpath("specimens")
    with resources.as_file(traversable) as p:
        if not p.exists() or not p.is_dir():
            raise FileNotFoundError(
                f"Specimens directory not found in package resources: {p}",
            )
        return p


def resolve_manifest_arg(arg: str | None, base: Path | None = None) -> Path | None:
    """Resolve a specimen identifier or path to its manifest.yaml.

    Args:
        arg: Specimen ID like "ducktape/2025-11-20-adgn" or filesystem path
        base: Specimens base directory (defaults to find_specimens_base())

    Returns:
        Path to manifest.yaml or None if not found
    """
    if arg is None:
        return None
    path = Path(arg)
    if path.exists():
        if path.is_dir():
            yaml_cand = path / "manifest.yaml"
            return yaml_cand if yaml_cand.exists() else None
        return path if path.suffix.lower() in {".yaml", ".yml"} else None
    base_dir = base or find_specimens_base()
    # Try direct hierarchical path (repo/name)
    yaml_cand = base_dir / arg.replace("/", os.sep) / "manifest.yaml"
    if yaml_cand.exists():
        return yaml_cand
    # Try prefix matching for convenience
    matches = [n for n in list_specimen_names(base_dir) if n.startswith(arg)]
    if len(matches) == 1:
        mdir = base_dir / matches[0].replace("/", os.sep)
        return (mdir / "manifest.yaml") if (mdir / "manifest.yaml").exists() else None
    return None


@dataclass(frozen=True)
class SpecimenRecord:
    slug: str
    manifest_path: Path
    manifest: SpecimenDoc
    issues: dict[str, IssueRecord]
    false_positives: dict[str, IssueRecord]
    known_files: dict[Path, FileType]  # File map from hydration (for complete validation contexts)

    @asynccontextmanager
    async def hydrated_copy(self, gitconfig: Path | None = None) -> AsyncIterator[Path]:
        """Yield a fresh private working tree path under $HOME for Docker-friendly mounts; clean up on exit.

        On macOS/Docker Desktop, mounts must be under $HOME to be shared with the VM. We therefore extract/copy under
        ~/.cache/adgn-llm/workspaces/<slug>_<ts>/ and yield the single extracted top-level directory.
        """
        gitconfig = gitconfig or _default_gitconfig()
        # Build a Docker-friendly mount root under $HOME
        mount_base = Path.home() / ".cache" / "adgn-llm" / "workspaces"
        mount_base.mkdir(parents=True, exist_ok=True)
        mount_root = mount_base / f"{self.slug}_{int(time.time())}"
        if mount_root.exists():
            shutil.rmtree(mount_root, ignore_errors=True)
        mount_root.mkdir(parents=True, exist_ok=True)

        # Materialize contents into mount_root according to source
        try:
            if isinstance(self.manifest.source, GitHubSource | GitSource):
                archive = ensure_archive_for_specimen_slug(
                    self.manifest,
                    self.manifest_path,
                    gitconfig,
                )
                with tarfile.open(archive, "r:gz") as tf:
                    members = [m for m in tf.getmembers() if ".git" not in m.name.split("/")]
                    if os.environ.get("ADGN_DEBUG_SPECIMEN") == "1":
                        total = len(tf.getmembers())
                        filtered = len(members)
                        logger.debug(
                            "[specimen] extracting %s members=%s/%s (filtered .git)",
                            archive,
                            filtered,
                            total,
                        )
                    tf.extractall(mount_root, members=members, filter=_specimen_extract_filter)
                    if os.environ.get("ADGN_DEBUG_SPECIMEN") == "1":
                        git_dirs = list((mount_root).rglob(".git"))
                        logger.debug("[specimen] post-extract .git dirs: %d", len(git_dirs))
                        for p in git_dirs[:10]:
                            logger.debug("    %s", p)
            elif isinstance(self.manifest.source, LocalSource):
                src = (self.manifest_path.parent / self.manifest.source.root).resolve()
                # For local specimens, materialize directly into mount_root (no extra subdir)
                shutil.copytree(src, mount_root, dirs_exist_ok=True)
            else:  # pragma: no cover - guarded by SpecimenDoc model
                raise SystemExit(
                    f"Unsupported source type: {type(self.manifest.source)}",
                )

            # Determine content root:
            # - If exactly one directory and no files: use that directory (typical for tarball extractions)
            # - Otherwise (e.g., local specimens copied directly): use mount_root itself
            all_entries = list(mount_root.iterdir())
            dirs = [p for p in all_entries if p.is_dir()]
            files = [p for p in all_entries if p.is_file()]
            content_root = dirs[0] if (len(dirs) == 1 and not files) else mount_root
            yield content_root
        finally:
            shutil.rmtree(mount_root, ignore_errors=True)

    def _validation_context(self) -> dict:
        """Build complete validation context for specimen.

        Returns:
            Dict with "specimen_context" key for model_validate(..., context=...)
        """
        ctx = SpecimenContext(
            specimen_slug=self.slug,
            known_files=self.known_files,  # Use stored file map for complete context
            allowed_tp_ids=self.issues.keys(),
            allowed_fp_ids=self.false_positives.keys(),
        )
        return {"specimen_context": ctx}

    @property
    def canonical_issues(self) -> list[CanonicalIssue]:
        """Canonical true positive issues with typed namespaced IDs."""
        return [
            CanonicalIssue(
                id=TruePositiveID(record.core.id),
                rationale=record.core.rationale,
                occurrences=record.instances,
            )
            for record in self.issues.values()
        ]

    @property
    def known_false_positives(self) -> list[KnownFalsePositive]:
        """Known false positive issues with typed namespaced IDs."""
        return [
            KnownFalsePositive(
                id=FalsePositiveID(record.core.id),
                rationale=record.core.rationale,
                occurrences=record.instances,
            )
            for record in self.false_positives.values()
        ]


class SpecimenRegistry:
    """Entry point for listing and obtaining specimen records (code-only facade).

    DI-friendly: pass in a preloaded mapping for tests; use load_* in app code.
    """

    def __init__(self, specimens: dict[str, SpecimenRecord]) -> None:
        # No I/O here; accept fully materialized data
        self._specimens = specimens

    @classmethod
    @asynccontextmanager
    async def load_and_hydrate(
        cls,
        slug: str,
        base: Path | None = None,
        gitconfig: Path | None = None,
    ) -> AsyncIterator[tuple[SpecimenRecord, Path]]:
        """Load specimen with validation and yield record + hydrated root together.

        Avoids double-hydration when caller needs both loaded issues and hydrated specimen.

        Yields:
            (SpecimenRecord, hydrated_root_path): Validated specimen and its hydrated working tree

        Example:
            async with SpecimenRegistry.load_and_hydrate("ducktape/2025-11-20-00") as (rec, root):
                await run_critic_agent(specimen_rec=rec, content_root=root, ...)
        """
        base_dir = base or find_specimens_base()
        manifest_path = (base_dir / slug.replace("/", os.sep) / "manifest.yaml").resolve()
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise SystemExit(f"Manifest must be a mapping: {manifest_path}")
        man = SpecimenDoc.model_validate(raw)

        # Evaluate Jsonnet to raw dicts (no validation yet)
        raw_issues = _jsonnet_evaluate_to_dict(manifest_path.parent, "issues", should_flag=True)
        raw_fps = _jsonnet_evaluate_to_dict(manifest_path.parent, "false_positives", should_flag=False)

        if raw_issues is None:
            raise SpecimenIssuesLoadError([f"No issues/ directory found under: {manifest_path.parent}"])
        if raw_fps is None:
            raw_fps = {}  # FPs are optional

        # Hydrate specimen to build complete validation context
        gitconfig = gitconfig or _default_gitconfig()
        hydrated_root = resolve_source_root(man, manifest_path, gitconfig)
        try:
            # Build complete context: files from hydration + IDs from Jsonnet
            known_files = {p.relative_to(hydrated_root): classify_path(p) for p in hydrated_root.rglob("*")}
            ctx = SpecimenContext(
                specimen_slug=slug,
                known_files=known_files,
                allowed_tp_ids=list(raw_issues.keys()),  # IDs from Jsonnet (strings)
                allowed_fp_ids=list(raw_fps.keys()),  # IDs from Jsonnet (strings)
            )
            context_dict = {"specimen_context": ctx}

            # Validate with complete context (both paths and IDs)
            res_pos = _validate_issues_from_dicts(raw_issues, context_dict, strict=True)
            res_fp = _validate_issues_from_dicts(raw_fps, context_dict, strict=True)

            if res_pos.errors or res_fp.errors:
                raise SpecimenIssuesLoadError([*res_pos.errors, *res_fp.errors])

            # Create record with stored file map
            rec = SpecimenRecord(
                slug=slug,
                manifest_path=manifest_path,
                manifest=man,
                issues={it.core.id: it for it in res_pos.items},
                false_positives={it.core.id: it for it in res_fp.items},
                known_files=known_files,  # Store for complete validation contexts
            )

            # Yield both - caller can use hydrated specimen without re-hydrating
            yield rec, hydrated_root
        finally:
            # Clean up hydrated specimen
            shutil.rmtree(
                hydrated_root.parent if hydrated_root.parent.name.startswith("adgn-specimen-") else hydrated_root,
                ignore_errors=True,
            )

    @classmethod
    async def load_strict(cls, slug: str, base: Path | None = None, gitconfig: Path | None = None) -> SpecimenRecord:
        """Load a specimen, raising SpecimenIssuesLoadError on any validation errors.

        .. deprecated::
            Use `load_and_hydrate()` directly instead. This wrapper discards the hydrated content root.

        Hydrates temporarily for validation, then cleans up.
        For use cases that need both specimen and hydrated root, use load_and_hydrate() instead.
        """
        warnings.warn(
            "load_strict() is deprecated. Use load_and_hydrate() context manager directly.",
            DeprecationWarning,
            stacklevel=2
        )
        async with cls.load_and_hydrate(slug, base, gitconfig) as (rec, _):
            return rec

    @classmethod
    def load_manifest_only(
        cls,
        slug: str,
        base: Path | None = None,
    ) -> tuple[Path, SpecimenDoc]:
        """Load only the manifest (no Jsonnet issues) for fast collection.

        Returns: (manifest_path, manifest_doc)
        Raises: SystemExit on manifest errors
        """
        base_dir = base or find_specimens_base()
        manifest_path = (base_dir / slug.replace("/", os.sep) / "manifest.yaml").resolve()
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise SystemExit(f"Manifest must be a mapping: {manifest_path}")

        manifest = SpecimenDoc.model_validate(raw)
        return (manifest_path, manifest)

    @classmethod
    async def load_lenient(
        cls,
        slug: str,
        base: Path | None = None,
        gitconfig: Path | None = None,
    ) -> tuple[SpecimenRecord, list[str]]:
        """Load a specimen by hierarchical ID (e.g., 'ducktape/2025-11-20-adgn').

        .. deprecated::
            Use `load_and_hydrate()` directly instead. This wrapper has no lenient behavior
            (just re-raises exceptions) and discards the hydrated content root.

        Returns specimen and any non-fatal errors encountered during loading.
        Hydrates temporarily for validation, then cleans up.
        For use cases that need both specimen and hydrated root, use load_and_hydrate() instead.
        """
        warnings.warn(
            "load_lenient() is deprecated. Use load_and_hydrate() context manager directly.",
            DeprecationWarning,
            stacklevel=2
        )
        try:
            async with cls.load_and_hydrate(slug, base, gitconfig) as (rec, _):
                return rec, []
        except SpecimenIssuesLoadError:
            # load_and_hydrate raises on errors; convert to lenient format
            # Re-run without strict mode by catching and returning errors
            # For now, just propagate (lenient mode not really different in new impl)
            raise

    @property
    def specimen_ids(self) -> list[str]:
        return sorted(self._specimens.keys())
