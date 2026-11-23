"""Copy strategies for worktree operations."""

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from x.wt.shared.configuration import CowMethod

# Top-level entries excluded by both copy strategies.
EXCLUDE_NAMES: tuple[str, ...] = (".git", ".worktrees")


def _copyable_entries(src: Path, parent: Path, excluded_names: set[str]) -> list[Path]:
    return [parent / child.name for child in (src / parent).iterdir() if child.name not in excluded_names]


class CopyStrategy(ABC):
    @abstractmethod
    def copy(self, src: Path, dst: Path, *, exclude: Path | None = None) -> None:
        """Copy src to dst, omitting the optional path relative to src."""

    @property
    @abstractmethod
    def method_name(self) -> str:
        pass


class ReflinkCopyStrategy(CopyStrategy):
    def copy(self, src: Path, dst: Path, *, exclude: Path | None = None) -> None:
        parent = Path()
        parents: list[Path] = []
        entries: list[Path] = []
        excluded_names = set(EXCLUDE_NAMES)
        if exclude is not None:
            # Enumerate siblings only along the excluded path. Let cp recurse into
            # every ordinary subtree in one invocation, preserving hardlinks.
            for name in exclude.parts[:-1]:
                child = parent / name
                if name in excluded_names or not (src / child).is_dir(follow_symlinks=False):
                    break
                entries.extend(_copyable_entries(src, parent, excluded_names | {name}))
                parents.append(child)
                parent = child
                excluded_names.clear()
            else:
                excluded_names.add(exclude.name)
        entries.extend(_copyable_entries(src, parent, excluded_names))

        # Preserve ancestors that will be empty once the excluded entry is skipped.
        for directory in parents:
            dest = dst / directory
            if dest.is_symlink():
                raise FileExistsError(f"Cannot copy through destination symlink: {dest}")
            dest.mkdir(exist_ok=True)
        if entries:
            subprocess.run(
                ["cp", "--archive", "--reflink=auto", "--parents", "--", *entries, dst.absolute()], cwd=src, check=True
            )
        for directory in reversed(parents):
            shutil.copystat(src / directory, dst / directory, follow_symlinks=False)

    @property
    def method_name(self) -> str:
        return "CoW reflink"


class RsyncCopyStrategy(CopyStrategy):
    def copy(self, src: Path, dst: Path, *, exclude: Path | None = None) -> None:
        # Protect root entries of any type, including a linked worktree's .git file.
        # Keep the existing recursive directory exclusions.
        exclude_args = [f"--exclude={pattern}" for name in EXCLUDE_NAMES for pattern in (f"/{name}", f"{name}/")]
        if exclude is not None:
            pattern = exclude.as_posix()
            # Rsync treats backslash literally unless the pattern has a glob character.
            if any(char in pattern for char in "*?["):
                for char in "\\*?[":
                    pattern = pattern.replace(char, "\\" + char)
            exclude_args.append(f"--exclude=/{pattern}")
        subprocess.run(["rsync", "-a", "--delete", *exclude_args, f"{src}/", f"{dst}/"], check=True)

    @property
    def method_name(self) -> str:
        return "rsync copy"


def _test_reflink_support() -> bool:
    if not shutil.which("cp"):
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_file = tmpdir_path / "test_src.txt"
        test_copy = tmpdir_path / "test_dst.txt"

        # Create a test file
        test_file.write_text("test content")

        # Try to copy with reflink
        try:
            subprocess.run(["cp", "--reflink=auto", test_file, test_copy], check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError, FileNotFoundError:
            return False


def get_copy_strategy(cow_method=None) -> CopyStrategy:
    """Get copy strategy based on cow_method preference or auto-detection."""

    # If cow_method is specified and not AUTO, try to use it
    if cow_method and cow_method != CowMethod.AUTO:
        return _get_strategy_for_method(cow_method)

    # Auto-detection logic (default behavior)
    if _test_reflink_support():
        return ReflinkCopyStrategy()
    return RsyncCopyStrategy()


def _get_strategy_for_method(cow_method) -> CopyStrategy:
    """Get strategy for specific CowMethod, with availability validation."""
    if cow_method == CowMethod.REFLINK:
        if _test_reflink_support():
            return ReflinkCopyStrategy()
        raise RuntimeError("Reflink copy is not supported on this system")

    if cow_method == CowMethod.COPY:
        # "copy" uses reflink when the filesystem supports it.
        if _test_reflink_support():
            return ReflinkCopyStrategy()
        return RsyncCopyStrategy()

    if cow_method == CowMethod.RSYNC:
        if not shutil.which("rsync"):
            raise RuntimeError("rsync is not available on this system")
        return RsyncCopyStrategy()

    raise RuntimeError(f"Unknown copy method: {cow_method}")
