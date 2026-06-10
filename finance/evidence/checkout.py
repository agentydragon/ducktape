"""Locate or clone the augur-evidence checkout.

The augur-evidence scraper maintains raw upstream data files in a private
Forgejo repo. `AUGUR_EVIDENCE_DIR` (the same variable augur's fit pipeline
uses) points at an existing checkout and wins; otherwise a shallow clone is
made once under `~/.cache/loom/augur-evidence` and reused as-is afterwards.
HTTP Basic creds (`AUGUR_EVIDENCE_GIT_USERNAME`/`AUGUR_EVIDENCE_GIT_PASSWORD`)
are needed only for that first clone — not when the cache already exists. In a
Claude agent session they come from the `claude` Forgejo service account (which
has read-only collaboration on the repo) — k8s Secret
`claude-sandbox/claude-forgejo-credentials`.
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
    checkout = Path.home() / ".cache" / "loom" / "augur-evidence"
    # A cached clone is reused as-is — no credentials needed once it exists.
    if (checkout / ".git").exists():
        return checkout
    # Cloning needs read credentials; they're required only on this first-time path.
    username = os.environ.get("AUGUR_EVIDENCE_GIT_USERNAME")
    password = os.environ.get("AUGUR_EVIDENCE_GIT_PASSWORD")
    if username is None or password is None:
        raise RuntimeError(
            "set AUGUR_EVIDENCE_DIR to an existing augur-evidence checkout, or set "
            "AUGUR_EVIDENCE_GIT_USERNAME / AUGUR_EVIDENCE_GIT_PASSWORD. In a Claude agent "
            "session the `claude` Forgejo account has read access:\n"
            "  U=$(kubectl get secret -n claude-sandbox claude-forgejo-credentials -o jsonpath='{.data.username}' | base64 -d)\n"
            "  P=$(kubectl get secret -n claude-sandbox claude-forgejo-credentials -o jsonpath='{.data.password}' | base64 -d)\n"
            "  export AUGUR_EVIDENCE_GIT_USERNAME=$U AUGUR_EVIDENCE_GIT_PASSWORD=$P"
        )
    logger.info("cloning evidence repo to %s", checkout)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _git_with_auth(["clone", "--depth", "1", EVIDENCE_REPO_URL, str(checkout)], username, password)
    return checkout
