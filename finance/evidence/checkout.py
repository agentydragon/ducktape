"""Locate or clone the augur-evidence checkout.

The augur-evidence scraper maintains raw upstream data files in a private
Forgejo repo. `AUGUR_EVIDENCE_DIR` (the same variable augur's fit pipeline
uses) points at an existing checkout and wins; otherwise a shallow clone is
kept under `~/.cache/loom/augur-evidence` using the `augur-evidence-reader`
credentials (k8s Secret `augur-evidence-git-read`, reflected into
`claude-sandbox`).
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

EVIDENCE_REPO_URL = "https://git.allegedly.works/augur-evidence/augur-evidence.git"


def _git_with_auth(arguments: list[str], username: str, password: str) -> None:
    # Credentials travel via git's environment-config mechanism, not argv, so
    # they don't leak into process listings or subprocess error messages.
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    environment = os.environ | {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
    }
    subprocess.run(["git", *arguments], check=True, env=environment)


def ensure_checkout() -> Path:
    if (configured := os.environ.get("AUGUR_EVIDENCE_DIR")) is not None:
        return Path(configured)
    username = os.environ.get("AUGUR_EVIDENCE_GIT_USERNAME")
    password = os.environ.get("AUGUR_EVIDENCE_GIT_PASSWORD")
    if username is None or password is None:
        raise RuntimeError(
            "set AUGUR_EVIDENCE_DIR to an existing augur-evidence checkout, or set "
            "AUGUR_EVIDENCE_GIT_USERNAME / AUGUR_EVIDENCE_GIT_PASSWORD "
            "(from `kubectl get secret augur-evidence-git-read -n claude-sandbox`)"
        )
    checkout = Path.home() / ".cache" / "loom" / "augur-evidence"
    if (checkout / ".git").exists():
        logger.info("updating evidence checkout at %s", checkout)
        _git_with_auth(["-C", str(checkout), "pull", "--ff-only"], username, password)
    else:
        logger.info("cloning evidence repo to %s", checkout)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _git_with_auth(["clone", "--depth", "1", EVIDENCE_REPO_URL, str(checkout)], username, password)
    return checkout
