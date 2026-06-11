"""Pure serializable configuration data model.

DO NOT ADD LOGIC - THIS IS PURE DATA

This module contains only the serializable Pydantic model that represents
the configuration data as stored in YAML files. No business logic, no
resolvers, no computed properties - just the raw data structure.

For runtime configuration with logic and resolvers, see configuration.py.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ConfigFile(BaseModel):
    """Pure serializable configuration data model.

    DO NOT ADD LOGIC - THIS IS PURE DATA

    This model represents the exact structure of the YAML configuration file.
    All fields should be basic Python types that can be serialized to/from YAML.
    No Path objects, no complex validation, no computed properties.
    """

    # Directory paths (as strings for serialization)
    main_repo: str  # Now required - explicit path to main repository
    worktrees_dir: str  # Absolute path

    # Git settings
    branch_prefix: str
    upstream_branch: str

    # Behavior settings
    log_operations: bool = False
    cow_method: Literal["auto", "reflink", "copy", "rsync"] = "auto"
    hydrate_worktrees: bool = True

    # GitHub integration
    github_enabled: bool = True
    github_repo: str = ""  # Format: "owner/repo"

    # Tool paths
    gitstatusd_path: str | None = None
    post_creation_script: str | None = None

    # Cache settings
    cache_expiration: int = 3600  # seconds
    cache_refresh_age: int = 300  # seconds

    # UI settings
    hidden_worktree_patterns: list[str] = Field(default_factory=list)

    # GitHub refresh system configuration
    github_debounce_delay: float = 5.0
    github_periodic_interval: float = 60.0

    # Daemon settings
    startup_timeout: float = 15.0  # seconds to wait for daemon startup

    # Hook/script settings
    post_creation_timeout: float = 60.0  # seconds to wait for post-creation script

    # Filesystem watcher settings
    git_watcher_debounce_delay: float = 0.5  # seconds to debounce .git changes for status refresh
