"""Shared resources for claude_hooks package."""

import importlib.resources
from importlib.resources.abc import Traversable

# Config files bundled with the package (templates, etc.)
CONFIG_FILES: Traversable = importlib.resources.files("claude_hooks.config")
