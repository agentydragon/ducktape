import datetime
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml


class PreCommitRunner:
    """Runner for pre-commit hooks based on provided config.

    This simplified version works both in and out of git repositories
    by using pre-commit's native capabilities.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def run(self, paths: Sequence[str | Path], cwd: str | Path | None = None) -> tuple[int, str, str]:
        """Run pre-commit hooks on specified paths.

        Args:
            paths: List of file paths to check
            cwd: Working directory (defaults to current directory)

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Ensure cwd is set
        current_working_dir = Path(cwd) if cwd else Path.cwd()

        # Only create debug logs if explicitly requested via environment variable
        debug_enabled = os.environ.get("CLAUDE_LINTER_DEBUG", "").lower() in ("1", "true", "yes")
        log_file = None
        if debug_enabled:
            # Create log file
            log_dir = Path.home() / ".cache" / "claude-linter"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"debug-{datetime.datetime.now().isoformat()}.log"

        # Create a temporary git repo to make pre-commit happy
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Initialize a git repo
            subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=False)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"], cwd=tmpdir, capture_output=True, check=False
            )
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, capture_output=True, check=False)

            # Copy files to temp repo, preserving full path structure
            temp_paths = []
            for path_input in paths:
                path = Path(path_input)
                # Get absolute path
                abs_path = path.absolute()

                # Find the git root of the original file (if in a git repo)
                try:
                    git_root_result = subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],
                        cwd=str(abs_path.parent),
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    original_git_root = Path(git_root_result.stdout.strip())
                    # Preserve the full path relative to the original git root
                    rel_path = abs_path.relative_to(original_git_root)
                except (subprocess.CalledProcessError, ValueError):
                    # Not in a git repo or can't determine relative path
                    # Fall back to using the full absolute path structure
                    rel_path = Path(*abs_path.parts[1:])  # Skip the root '/'

                temp_file = tmpdir_path / rel_path
                temp_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file.write_text(path.read_text())
                temp_paths.append(str(rel_path))

            # Always use the config as-is (always fix=True)
            config = self.config

            # Write config file
            config_path = tmpdir_path / ".pre-commit-config.yaml"
            config_text = yaml.dump(config)
            config_path.write_text(config_text)

            # Write debug info to log if enabled
            if log_file:
                with log_file.open("w") as f:
                    f.write("=== Claude Linter Debug Log ===\n")
                    f.write(f"Time: {datetime.datetime.now()}\n")
                    f.write(f"Working dir: {current_working_dir}\n")
                    f.write(f"Temp dir: {tmpdir}\n")
                    f.write(f"Paths: {paths}\n")
                    f.write(f"Temp paths: {temp_paths}\n")
                    f.write(f"\n--- Config ---\n{config_text}\n")

                    # Write file contents
                    f.write("\n--- File contents ---\n")
                    for temp_path in temp_paths:
                        file_path = tmpdir_path / temp_path
                        f.write(f"\n{temp_path}:\n")
                        if file_path.exists():
                            f.write(file_path.read_text())
                        else:
                            f.write("(file does not exist)\n")

            # Stage files
            subprocess.run(["git", "add", *temp_paths], cwd=tmpdir, capture_output=True, check=False)

            # Build command
            cmd = [
                "pre-commit",
                "run",
                "--all-files",  # Safe since we're in a temp dir with only our files
                "--verbose",
                # Use default stage (pre-commit)
            ]

            # Run as subprocess
            result = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True, check=False)

            # Append results to log if enabled
            if log_file:
                with log_file.open("a") as f:
                    f.write("\n--- Pre-commit command ---\n")
                    f.write(f"Command: {' '.join(cmd)}\n")
                    f.write(f"Return code: {result.returncode}\n")
                    f.write(f"\n--- Stdout ---\n{result.stdout}\n")
                    f.write(f"\n--- Stderr ---\n{result.stderr}\n")

            # Copy modified files back
            for i, rel_path_str in enumerate(temp_paths):
                temp_file = tmpdir_path / rel_path_str
                if temp_file.exists():
                    # Copy back to original location
                    original_path = Path(paths[i])
                    original_path.write_text(temp_file.read_text())

            return result.returncode, result.stdout, result.stderr
