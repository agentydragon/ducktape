"""Copy strategies for worktree operations."""

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path

from devinfra.wt.shared.configuration import CowMethod

# Unified list of top-level entries to exclude when copying a worktree directory
# Keep in sync with rsync excludes
EXCLUDE_NAMES: tuple[str, ...] = (".git", ".worktrees")


def _get_copyable_entries(src: Path) -> list[Path]:
    """List top-level entries to copy from src, excluding repo internals.

    Excludes items in EXCLUDE_NAMES for all strategies to keep behavior consistent
    across platforms and tools.
    """
    return [child for child in src.iterdir() if child.name not in EXCLUDE_NAMES]


class StrategyType(StrEnum):
    """Copy strategy types."""

    REFLINK = "reflink"
    RSYNC = "rsync"


class CopyStrategy(ABC):
    @abstractmethod
    def copy(self, src: Path, dst: Path) -> None:
        pass

    @property
    @abstractmethod
    def method_name(self) -> str:
        pass

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        pass


class ReflinkCopyStrategy(CopyStrategy):
    def copy(self, src: Path, dst: Path) -> None:
        entries = _get_copyable_entries(src)
        if entries:
            subprocess.run(["cp", "--archive", "--reflink=auto", *entries, dst], check=True)

    @property
    def method_name(self) -> str:
        return "CoW reflink"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.REFLINK


class RsyncCopyStrategy(CopyStrategy):
    def copy(self, src: Path, dst: Path) -> None:
        exclude_args = [f"--exclude={name}/" for name in EXCLUDE_NAMES]
        subprocess.run(["rsync", "-a", "--delete", *exclude_args, f"{src}/", f"{dst}/"], check=True)

    @property
    def method_name(self) -> str:
        return "rsync copy"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.RSYNC


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
        except (subprocess.CalledProcessError, FileNotFoundError):
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
