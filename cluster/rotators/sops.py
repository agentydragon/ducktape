"""Encrypt rotation outputs using repository-relative SOPS rules."""

import subprocess
from pathlib import Path


def encrypt_sops_file(path: Path) -> None:
    subprocess.run(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], check=True)
