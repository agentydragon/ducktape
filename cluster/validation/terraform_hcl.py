"""Read Terraform `locals` blocks via pygohcl (HashiCorp's HCL2 parser)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pygohcl


def locals_blocks(path: Path) -> list[dict[str, Any]]:
    """Parse `path` and return its `locals {}` blocks as a list of dicts.

    pygohcl collapses a single `locals {}` block to a dict and keeps multiple blocks as a
    list of dicts; normalize to a list so callers can iterate uniformly.
    """
    blocks = pygohcl.loads(path.read_text()).get("locals", [])
    return [blocks] if isinstance(blocks, dict) else blocks
