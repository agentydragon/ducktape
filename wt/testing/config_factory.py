"""Configuration factory to reduce duplication in test configuration building."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from wt.shared.config_file import ConfigFile
from wt.shared.configuration import Configuration
from wt.testing.data import WATCHER_DEBOUNCE_SECS, ConfigPresets, TestData


class ConfigFactory:
    """Factory for creating test configurations with different presets."""

    def __init__(self, repo_path: Path, temp_base_dir: Path | None = None):
        """Initialize factory with repository path."""
        self.repo_path = repo_path
        self.temp_base_dir = temp_base_dir or repo_path.parent

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
        default_config = {
            "main_repo": str(self.repo_path),
            "worktrees_dir": str(self.repo_path / TestData.Paths.WORKTREES_DIR_NAME),
            "branch_prefix": TestData.Branches.TEST_PREFIX,
            "upstream_branch": TestData.Branches.MAIN,
            "github_repo": "test-user/test-repo",
            "github_enabled": False,
            "log_operations": True,
            "cache_expiration": 3600,
            "cache_refresh_age": 300,
            "hidden_worktree_patterns": [],
            "cow_method": "copy",
            "gitstatusd_path": None,  # Will be filled by tests that need it
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


class ConfigBuilder:
    """Builder pattern for more complex configuration creation."""

    def __init__(self, repo_path: Path):
        """Initialize builder with repository path."""
        self.factory = ConfigFactory(repo_path)
        self._overrides: dict[str, Any] = {}
        self._preset = "MINIMAL"

    def with_preset(self, preset: str):
        """Set the configuration preset."""
        self._preset = preset
        return self

    def with_github(self, repo: str = "test-user/test-repo", enabled: bool = True):
        """Configure GitHub integration."""
        self._overrides.update({"github_enabled": enabled, "github_repo": repo})
        return self

    def with_worktrees_dir(self, path: str | Path):
        """Set custom worktrees directory."""
        self._overrides["worktrees_dir"] = str(path)
        return self

    def with_branch_prefix(self, prefix: str):
        """Set custom branch prefix."""
        self._overrides["branch_prefix"] = prefix
        return self

    def with_upstream_branch(self, branch: str):
        """Set custom upstream branch."""
        self._overrides["upstream_branch"] = branch
        return self

    def with_cow_method(self, method: str):
        """Set copy-on-write method."""
        self._overrides["cow_method"] = method
        return self

    def with_gitstatusd_path(self, path: str | Path):
        """Set gitstatusd binary path."""
        self._overrides["gitstatusd_path"] = str(path)
        return self

    def with_post_creation_script(self, script_path: str | Path):
        """Set post-creation script path."""
        self._overrides["post_creation_script"] = str(script_path)
        return self

    def with_custom_field(self, field_name: str, value: Any):
        """Set any custom configuration field."""
        self._overrides[field_name] = value
        return self

    def build(self, wt_dir: Path | None = None) -> Configuration:
        """Build the final configuration."""
        return self.factory.create(self._preset, wt_dir=wt_dir, **self._overrides)
