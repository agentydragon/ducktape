"""Check that terraform modules don't define provider version constraints.

Provider versions should be centralized in root terraform.tf files, not in modules.
This prevents version conflicts and ensures consistent provider versions across the codebase.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MODULES_DIR = Path("cluster/terraform/modules")
REQUIRED_PROVIDERS_RE = re.compile(r"required_providers\s*\{")
VERSION_CONSTRAINT_RE = re.compile(r"^\s*version\s*=", re.MULTILINE)


def has_version_in_required_providers(content: str) -> bool:
    """Check if content has version constraints inside required_providers blocks."""
    return bool(REQUIRED_PROVIDERS_RE.search(content) and VERSION_CONSTRAINT_RE.search(content))


def find_violations() -> list[Path]:
    """Find .tf files in modules with provider version constraints."""
    if not MODULES_DIR.exists():
        return []

    return [
        tf_file
        for tf_file in MODULES_DIR.rglob("*.tf")
        if tf_file.name != "terraform.tf"
        and ".terraform" not in tf_file.parts
        and has_version_in_required_providers(tf_file.read_text())
    ]


def main() -> int:
    if violations := find_violations():
        print("FATAL: Provider version constraints found in modules (should be in root terraform.tf):")
        for path in violations:
            print(f"  {path}")
        print("\nModules should not specify provider versions - only root modules should.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
