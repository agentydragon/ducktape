"""Shared file utilities for runners."""

import os
from pathlib import Path
from typing import ClassVar, Protocol


class FileCollectionConfig:
    """Configuration for file collection across all runners."""

    # Directories to skip when collecting files
    SKIP_DIRS: ClassVar[set[str]] = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".pytest_cache",
        ".coverage",
        ".mypy_cache",
        ".tox",
        ".eggs",
        "*.egg-info",
        "dist",
        "build",
        ".DS_Store",
    }

    # File extensions to skip
    SKIP_EXTENSIONS: ClassVar[set[str]] = {
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".lib",
        ".exe",
        ".class",
        ".jar",
        ".log",
        ".tmp",
        ".temp",
    }

    # Maximum file size to collect (1MB)
    MAX_FILE_SIZE_BYTES: int = 1024 * 1024


class FileProvider(Protocol):
    """Protocol for objects that can provide file listings and contents."""

    def list_files(self) -> list[str]:
        """List all files in the workspace (relative paths)."""
        ...

    def get_file_content(self, path: str) -> str:
        """Get content of a specific file by relative path."""
        ...


class WorkspaceFileProvider:
    """File provider for local workspace directories."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path

    def list_files(self) -> list[str]:
        """List all files in the workspace."""
        if not self.workspace_path or not self.workspace_path.exists():
            return []

        files = []
        for root, dirs, filenames in os.walk(self.workspace_path):
            # Remove directories we want to skip
            dirs[:] = [d for d in dirs if d not in FileCollectionConfig.SKIP_DIRS]

            for filename in filenames:
                # Skip certain file extensions
                if any(filename.endswith(ext) for ext in FileCollectionConfig.SKIP_EXTENSIONS):
                    continue

                filepath = Path(root) / filename
                relative_path = filepath.relative_to(self.workspace_path)

                # Skip very large files
                if filepath.stat().st_size > FileCollectionConfig.MAX_FILE_SIZE_BYTES:
                    continue

                files.append(str(relative_path))

        return files

    def get_file_content(self, path: str) -> str:
        """Get content of a file."""
        filepath = self.workspace_path / path
        try:
            with filepath.open(encoding="utf-8") as f:
                return f.read()
        except (UnicodeDecodeError, OSError) as e:
            raise ValueError(f"Cannot read file {path}") from e


class DockerFileProvider:
    """File provider for Docker container workspaces."""

    def __init__(self, container_files: list[dict[str, str]]):
        """container_files: List of dicts with 'path' and 'content' keys."""
        self._files = {f["path"]: f["content"] for f in container_files}

    def list_files(self) -> list[str]:
        """List all files."""
        return [path for path in self._files if self._should_include_file(path)]

    def get_file_content(self, path: str) -> str:
        """Get content of a file."""
        if path not in self._files:
            raise ValueError(f"File not found: {path}")
        return self._files[path]

    def _should_include_file(self, path: str) -> bool:
        """Check if file should be included based on filtering rules."""
        # Skip files in excluded directories
        path_parts = Path(path).parts
        for part in path_parts[:-1]:  # Exclude the filename itself
            if part in FileCollectionConfig.SKIP_DIRS:
                return False

        # Skip files with excluded extensions
        return not any(path.endswith(ext) for ext in FileCollectionConfig.SKIP_EXTENSIONS)


class FileCollector:
    """Unified file collector that works with any FileProvider."""

    def __init__(self, provider: FileProvider):
        self.provider = provider

    def collect_files(self) -> dict[str, str]:
        """Collect all files from the provider.

        Returns:
            Dictionary mapping relative file paths to their contents
        """
        files = {}

        for path in self.provider.list_files():
            try:
                content = self.provider.get_file_content(path)
                files[path] = content
            except (ValueError, OSError):
                # Skip files that can't be read
                continue

        return files


def collect_workspace_files(workspace_path: Path) -> dict[str, str]:
    """Returns dict mapping relative file paths to their contents."""
    provider = WorkspaceFileProvider(workspace_path)
    collector = FileCollector(provider)
    return collector.collect_files()


def collect_docker_files(container_files: list[dict[str, str]]) -> dict[str, str]:
    """Returns dict mapping relative file paths to their contents."""
    provider = DockerFileProvider(container_files)
    collector = FileCollector(provider)
    return collector.collect_files()
