"""Immutable configuration after resolution.

This module contains the frozen Configuration dataclass that represents
resolved configuration with all paths validated and computed upfront.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from hashlib import md5
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from wt.shared.config_file import ConfigFile
from wt.shared.env import is_test_mode

MAX_SOCK_PATH_LEN = 100


class CowMethod(StrEnum):
    """Copy-on-write methods for worktree hydration."""

    AUTO = "auto"
    REFLINK = "reflink"
    COPY = "copy"
    RSYNC = "rsync"


class ConfigError(Exception):
    """Configuration validation or loading error."""


@dataclass(frozen=True)
class Configuration:
    """Immutable configuration after resolution."""

    wt_dir: Path
    main_repo: Path
    worktrees_dir: Path
    branch_prefix: str
    upstream_branch: str
    github_repo: str
    github_enabled: bool
    log_operations: bool
    cow_method: CowMethod
    gitstatusd_path: Path | None
    post_creation_script: Path | None
    cache_expiration: timedelta
    cache_refresh_age: timedelta
    hidden_worktree_patterns: list[str]
    github_debounce_delay: timedelta
    github_periodic_interval: timedelta
    startup_timeout: timedelta
    post_creation_timeout: timedelta
    git_watcher_debounce_delay: timedelta
    hydrate_worktrees: bool = True

    @property
    def daemon_socket_path(self) -> Path:
        """Path to daemon UNIX socket with macOS length-safe fallback.

        Notes:
        - Use the real (resolved) path for length checks to avoid /var → /private/var
          symlink surprises on macOS. The kernel enforces the limit on the real path.
        - If too long, fall back to a stable short path under /tmp derived from WT_DIR.
        """
        real_wt_dir = self.wt_dir.resolve()
        p = real_wt_dir / "daemon.sock"
        if len(str(p)) <= MAX_SOCK_PATH_LEN:
            return p
        h = md5(str(real_wt_dir).encode()).hexdigest()[:12]
        return Path("/tmp") / f"wt_daemon_{h}.sock"

    @property
    def daemon_pid_path(self) -> Path:
        """Path to daemon PID file."""
        return self.wt_dir / "daemon.pid"

    @property
    def operations_log_file(self) -> Path:
        """Path to operations log file."""
        return self.wt_dir / "operations.log"

    @property
    def pr_cache_file(self) -> Path:
        """Path to PR cache file."""
        return self.wt_dir / "pr_cache.json"

    @property
    def daemon_log_file(self) -> Path:
        """Path to daemon log file."""
        return self.wt_dir / "daemon.log"

    @classmethod
    def resolve(cls, wt_dir: Path) -> Configuration:
        """Resolve configuration from WT_DIR - does all filesystem validation upfront."""
        config_path = wt_dir / "config.yaml"

        if not config_path.exists():
            raise ConfigError(f"Config file not found: {config_path}")

        try:
            config_file = ConfigFile.model_validate(yaml.safe_load(config_path.read_text()))
        except ValidationError as e:
            raise ConfigError("Configuration validation errors") from e

        # Resolve and validate all paths NOW
        main_repo = Path(config_file.main_repo).expanduser().resolve()
        if not main_repo.exists():
            raise ConfigError(f"Main repo not found: {main_repo}")
        if not (main_repo / ".git").exists():
            raise ConfigError(f"Not a git repository: {main_repo}")

        worktrees_dir = Path(config_file.worktrees_dir).expanduser().resolve()

        # Resolve optional paths
        gitstatusd_path = None
        if config_file.gitstatusd_path:
            gitstatusd_path = Path(config_file.gitstatusd_path).expanduser().resolve()

        post_creation_script = None
        if config_file.post_creation_script:
            post_creation_script = Path(config_file.post_creation_script).expanduser().resolve()

        cfg = cls(
            wt_dir=wt_dir,
            main_repo=main_repo,
            worktrees_dir=worktrees_dir,
            branch_prefix=config_file.branch_prefix,
            upstream_branch=config_file.upstream_branch,
            github_repo=config_file.github_repo,
            github_enabled=config_file.github_enabled,
            log_operations=config_file.log_operations,
            cow_method=CowMethod(config_file.cow_method),
            gitstatusd_path=gitstatusd_path,
            post_creation_script=post_creation_script,
            cache_expiration=timedelta(seconds=config_file.cache_expiration),
            cache_refresh_age=timedelta(seconds=config_file.cache_refresh_age),
            hidden_worktree_patterns=config_file.hidden_worktree_patterns.copy(),
            github_debounce_delay=timedelta(seconds=config_file.github_debounce_delay),
            github_periodic_interval=timedelta(seconds=config_file.github_periodic_interval),
            startup_timeout=timedelta(seconds=config_file.startup_timeout),
            post_creation_timeout=timedelta(seconds=config_file.post_creation_timeout),
            git_watcher_debounce_delay=timedelta(seconds=config_file.git_watcher_debounce_delay),
            hydrate_worktrees=config_file.hydrate_worktrees,
        )

        if is_test_mode():
            temp_root = Path(tempfile.gettempdir()).resolve()

            def _under_tmp(p: Path) -> bool:
                return p.resolve().is_relative_to(temp_root)

            if not (_under_tmp(cfg.wt_dir) and _under_tmp(cfg.main_repo) and _under_tmp(cfg.worktrees_dir)):
                raise ConfigError(
                    "WT_TEST_MODE is set, but WT_DIR/main_repo/worktrees_dir are not under the system temp directory.\n"
                    f"  WT_DIR={cfg.wt_dir}\n  main_repo={cfg.main_repo}\n  worktrees_dir={cfg.worktrees_dir}\n"
                    "Refusing to run tests against a non-isolated real environment."
                )
        return cfg


def load_config() -> Configuration:
    """Load configuration from WT_DIR environment variable."""
    wt_dir_env = os.getenv("WT_DIR")
    if not wt_dir_env:
        click.echo("Error: WT_DIR environment variable must be set")
        sys.exit(1)

    wt_dir = Path(wt_dir_env).expanduser().resolve()
    if not wt_dir.exists():
        click.echo(f"Error: WT_DIR does not exist: {wt_dir}")
        sys.exit(1)

    try:
        return Configuration.resolve(wt_dir)
    except ConfigError as e:
        click.echo(f"Configuration error: {e}")
        sys.exit(1)
