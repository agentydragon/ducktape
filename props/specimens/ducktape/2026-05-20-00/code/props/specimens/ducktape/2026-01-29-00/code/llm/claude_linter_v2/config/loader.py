"""Configuration loader for Claude Linter v2."""

import logging
from pathlib import Path
from typing import ClassVar

from llm.claude_linter_v2.config.clean_models import LogLevel, ModularConfig

logger = logging.getLogger(__name__)


class ConfigLoader:
    """Loads and manages Claude Linter v2 configuration."""

    DEFAULT_CONFIG_NAMES: ClassVar[tuple[str, ...]] = (
        ".claude-linter.toml",
        ".claude-linter-v2.toml",
        "claude-linter.toml",
    )

    def __init__(self, config_path: Path | None = None) -> None:
        """
        Initialize the config loader.

        Args:
            config_path: Explicit path to config file, or None to search
        """
        self.config_path = config_path
        self._config: ModularConfig | None = None

    def load(self) -> ModularConfig:
        """
        Load configuration from file or use defaults.

        Returns:
            Loaded configuration
        """
        if self._config is not None:
            return self._config

        # If explicit path provided
        if self.config_path:
            if self.config_path.exists():
                logger.info(f"Loading config from {self.config_path}")
                self._config = ModularConfig.from_toml(self.config_path)
                return self._config
            logger.warning(f"Config file not found: {self.config_path}")

        # Search for config file
        config_file = self._find_config_file()
        if config_file:
            logger.info(f"Loading config from {config_file}")
            self._config = ModularConfig.from_toml(config_file)
        else:
            logger.info("No config file found, using defaults")
            self._config = ModularConfig(version="2.0", max_errors_to_show=3, log_level=LogLevel.INFO, log_file=None)

        return self._config

    def _find_config_file(self) -> Path | None:
        """
        Search for a config file in the current directory and parents.

        Returns:
            Path to config file if found, None otherwise
        """
        current = Path.cwd()

        # Check current directory and all parents up to root
        while True:
            for name in self.DEFAULT_CONFIG_NAMES:
                config_path = current / name
                if config_path.exists():
                    return config_path

            # Stop at root or home directory
            if current == current.parent or current == Path.home():
                break

            current = current.parent

        return None

    def reload(self) -> ModularConfig:
        """
        Force reload of configuration.

        Returns:
            Reloaded configuration
        """
        self._config = None
        return self.load()

    @property
    def config(self) -> ModularConfig:
        """Get the loaded configuration."""
        if self._config is None:
            return self.load()
        return self._config
