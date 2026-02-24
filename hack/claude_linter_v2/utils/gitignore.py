"""Gitignore support for file filtering."""

import subprocess
from pathlib import Path


def get_git_tracked_files(directory: Path, pattern: str = "*.py") -> list[Path]:
    """
    Get files tracked by git that match the given pattern.

    Args:
        directory: Directory to search in
        pattern: File pattern to match (e.g., "*.py")

    Returns:
        List of paths that are tracked by git and match the pattern
    """
    try:
        # Use git ls-files to get tracked files
        # This automatically respects .gitignore
        result = subprocess.run(["git", "ls-files", pattern], cwd=directory, capture_output=True, text=True, check=True)

        # Parse the output into Path objects
        files = []
        for line in result.stdout.strip().split("\n"):
            if line:
                file_path = directory / line
                if file_path.exists():
                    files.append(file_path)

        return files
    except subprocess.CalledProcessError:
        # Not a git repository or git command failed
        # Fall back to all files (no gitignore filtering)
        return list(directory.rglob(pattern))
    except FileNotFoundError:
        # git not installed
        return list(directory.rglob(pattern))


def is_git_tracked(file_path: Path) -> bool:
    """
    Check if a file is tracked by git.

    Args:
        file_path: Path to check

    Returns:
        True if file is tracked by git, False otherwise
    """
    try:
        # Check if file is tracked
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(file_path)], capture_output=True, check=False
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Assume not tracked if we can't check
        return False
