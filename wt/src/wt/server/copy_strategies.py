"""Copy strategies for worktree operations."""

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from ..shared.configuration import CowMethod

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

    CLONEFILE = "clonefile"
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


class ClonefileCopyStrategy(CopyStrategy):
    def copy(self, src: Path, dst: Path) -> None:
        entries = _get_copyable_entries(src)
        if entries:
            # Prefer clonefile (-c) when available; fall back to plain recursive copy otherwise.
            args = ["cp", "-R", *map(str, entries), str(dst)]
            if _supports_cp_clone():
                args = ["cp", "-c", "-R", *map(str, entries), str(dst)]
            subprocess.run(args, check=True)

    @property
    def method_name(self) -> str:
        return "CoW clonefile"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.CLONEFILE


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


def _supports_cp_clone() -> bool:
    """Detect at runtime whether 'cp -c' (clonefile) is supported."""
    if not shutil.which("cp"):
        return False
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_file = tmpdir_path / "clone_src.txt"
        test_copy = tmpdir_path / "clone_dst.txt"
        test_file.write_text("x")
        try:
            subprocess.run(["cp", "-c", test_file, test_copy], check=True, capture_output=True, text=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


def get_copy_strategy(cow_method=None) -> CopyStrategy:
    """Get copy strategy based on cow_method preference or auto-detection."""

    # If cow_method is specified and not AUTO, try to use it
    if cow_method and cow_method != CowMethod.AUTO:
        return _get_strategy_for_method(cow_method)

    # Auto-detection logic (default behavior)
    if sys.platform == "darwin" and shutil.which("cp"):
        # Use clonefile on macOS only when supported; otherwise let detection continue.
        if _supports_cp_clone():
            return ClonefileCopyStrategy()
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
        # "copy" maps to clonefile on macOS, reflink elsewhere
        if sys.platform == "darwin" and shutil.which("cp"):
            return ClonefileCopyStrategy()
        if _test_reflink_support():
            return ReflinkCopyStrategy()
        return RsyncCopyStrategy()

    if cow_method == CowMethod.RSYNC:
        if not shutil.which("rsync"):
            raise RuntimeError("rsync is not available on this system")
        return RsyncCopyStrategy()

    raise RuntimeError(f"Unknown copy method: {cow_method}")
