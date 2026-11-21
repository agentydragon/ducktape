"""Write rotation outputs as SOPS-encrypted YAML using repository-relative SOPS rules."""

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from devinfra.prettier_cli import prettier_format_yaml_in_place


def write_sops_yaml(path: Path, document: Mapping[str, Any]) -> None:
    """Write `document` to `path`, encrypt it in place with SOPS, then prettier-format the result.

    Prettier runs last so the ciphertext the rotator commits already matches the repo's formatting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(document), sort_keys=False, width=2**31))
    subprocess.run(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], check=True)
    prettier_format_yaml_in_place(path)
