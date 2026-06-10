"""Auto-resolve augur-evidence git credentials for loom agent sessions.

`finance.evidence.checkout.ensure_checkout` needs `AUGUR_EVIDENCE_DIR` or
`AUGUR_EVIDENCE_GIT_USERNAME`/`AUGUR_EVIDENCE_GIT_PASSWORD`. In a Claude agent
session neither is set, but the `claude` Forgejo service account
(k8s `claude-sandbox/claude-forgejo-credentials`, provisioned by
`tf/gitops/forgejo-claude`) holds read-only collaboration on the
`augur-evidence` repo. Fetch those creds on demand — the same kubectl path
`devinfra/claude/claude_hook/creds_banner.sh` documents — so a loom run clones
the evidence repo with no manual setup.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_FORGEJO_SECRET = "claude-forgejo-credentials"
_SANDBOX_NAMESPACE = "claude-sandbox"


def _kubectl_secret_field(secret: str, namespace: str, field: str) -> str | None:
    result = subprocess.run(
        ["kubectl", "get", "secret", secret, "-n", namespace, "-o", f"jsonpath={{.data.{field}}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        logger.warning("kubectl read of %s/%s .%s failed: %s", namespace, secret, field, result.stderr.strip())
        return None
    return base64.b64decode(result.stdout).decode()


def ensure_evidence_git_creds() -> None:
    """Populate AUGUR_EVIDENCE_GIT_{USERNAME,PASSWORD} from the `claude` Forgejo account.

    No-op when AUGUR_EVIDENCE_DIR or the git creds are already set, or when the
    secret can't be read (``ensure_checkout`` then raises its own guidance).
    """
    if os.environ.get("AUGUR_EVIDENCE_DIR"):
        return
    if os.environ.get("AUGUR_EVIDENCE_GIT_USERNAME") and os.environ.get("AUGUR_EVIDENCE_GIT_PASSWORD"):
        return
    username = _kubectl_secret_field(_FORGEJO_SECRET, _SANDBOX_NAMESPACE, "username")
    password = _kubectl_secret_field(_FORGEJO_SECRET, _SANDBOX_NAMESPACE, "password")
    if username is None or password is None:
        return
    os.environ["AUGUR_EVIDENCE_GIT_USERNAME"] = username
    os.environ["AUGUR_EVIDENCE_GIT_PASSWORD"] = password
    logger.info("augur-evidence: using the claude Forgejo account from %s/%s", _SANDBOX_NAMESPACE, _FORGEJO_SECRET)
