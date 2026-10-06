"""Configuration factory to reduce duplication in test configuration building."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from util.bazel.runfiles import get_required_path
from x.wt.shared.config_file import ConfigFile
from x.wt.shared.configuration import Configuration
from x.wt.testing.data import WATCHER_DEBOUNCE_SECS, ConfigPresets, TestData

# Apparent repo name of the `gitstatusd` http_archive in MODULE.bazel.
_GITSTATUSD_RLOCATION = "gitstatusd/gitstatusd-linux-x86_64"


def gitstatusd_binary() -> Path:
    """The Bazel-provided gitstatusd; only test targets that list `//third_party/gitstatusd` in `data` have it."""
    return get_required_path(_GITSTATUSD_RLOCATION)


class ConfigFactory:
    """Factory for creating test configurations with different presets."""

    def __init__(self, repo_path: Path, temp_base_dir: Path | None = None, *, gitstatusd_path: Path | None = None):
        """Initialize factory with repository path.

        `gitstatusd_path` is written into the configurations of a test that starts a daemon; None leaves it unset.
        """
        self.repo_path = repo_path
        self.temp_base_dir = temp_base_dir or repo_path.parent
        self.gitstatusd_path = gitstatusd_path

    def create(
        self, preset: str | Mapping[str, Any] = "MINIMAL", *, wt_dir: Path | None = None, **config_overrides
    ) -> Configuration:
        """preset is a name from ConfigPresets class or a dict."""
        # Get base configuration from preset (by value or by name)
        if isinstance(preset, Mapping):
            base_config = dict(preset)
        elif hasattr(ConfigPresets, preset):
            base_config = getattr(ConfigPresets, preset)
        else:
            raise ValueError(f"Unknown preset: {preset}. Available: {self._available_presets()}")

        # Set up WT_DIR
        if wt_dir is None:
            wt_dir = self.temp_base_dir / TestData.Paths.TEST_WT_DIR_PARENT / TestData.Paths.WT_DIR_NAME

        # Create default configuration
        default_config: dict[str, Any] = {
            "main_repo": str(self.repo_path),
            "worktrees_dir": str(self.repo_path / TestData.Paths.WORKTREES_DIR_NAME),
            "branch_prefix": TestData.Branches.TEST_PREFIX,
            "upstream_branch": TestData.Branches.MAIN,
            "github_repo": None,
            "log_operations": True,
            "cache_expiration": 3600,
            "cache_refresh_age": 300,
            "hidden_worktree_patterns": [],
            "cow_method": "copy",
            # The daemon subprocess re-reads this from config.yaml, so it needs no runfiles access of its own.
            "gitstatusd_path": None if self.gitstatusd_path is None else str(self.gitstatusd_path),
            "post_creation_script": None,
            "git_watcher_debounce_delay": WATCHER_DEBOUNCE_SECS,
            # Keep daemon startup bounded well under per-test subprocess timeouts
            "startup_timeout": 4,
            # Keep post-creation hooks snappy in tests
            "post_creation_timeout": 20,
            # Lower debounce in tests for faster watcher reaction (prod default ~0.5s)
        }

        # Merge: default -> preset -> user overrides
        final_config = {**default_config, **base_config, **config_overrides}

        # Create ConfigFile and save to YAML
        config_file = ConfigFile(**final_config)
        return self._save_and_resolve(config_file, wt_dir)

    def minimal(self, **overrides) -> Configuration:
        """Create minimal configuration for fast tests."""
        return self.create(ConfigPresets.MINIMAL, **overrides)

    def integration(self, **overrides) -> Configuration:
        """Create configuration for integration tests."""
        return self.create(ConfigPresets.INTEGRATION, **overrides)

    def e2e(self, **overrides) -> Configuration:
        """Create configuration for end-to-end tests."""
        return self.create(ConfigPresets.E2E, **overrides)

    def with_github(self, **overrides) -> Configuration:
        """Create configuration with GitHub enabled."""
        return self.create(ConfigPresets.GITHUB_ENABLED, **overrides)

    def custom(self, **config_fields) -> Configuration:
        """Create configuration with all custom fields (no preset)."""
        # Start with minimal and override everything
        return self.create("MINIMAL", **config_fields)

    def _save_and_resolve(self, config_file: ConfigFile, wt_dir: Path) -> Configuration:
        """Save ConfigFile to YAML and resolve Configuration."""
        # Ensure WT_DIR exists
        wt_dir.mkdir(parents=True, exist_ok=True)

        # Ensure worktrees directory exists (critical for tests)
        worktrees_dir = Path(config_file.worktrees_dir)
        worktrees_dir.mkdir(parents=True, exist_ok=True)

        # Save configuration file
        config_path = wt_dir / TestData.Paths.CONFIG_FILE_NAME
        with config_path.open("w") as f:
            yaml.dump(config_file.model_dump(), f)

        # Resolve and return Configuration
        return Configuration.resolve(wt_dir)

    def _available_presets(self) -> list[str]:
        """Get list of available preset names."""
        return [
            name
            for name in dir(ConfigPresets)
            if not name.startswith("_") and isinstance(getattr(ConfigPresets, name), dict)
        ]
