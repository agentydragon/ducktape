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

import logging
import os
from pathlib import Path

import pygit2

logger = logging.getLogger(__name__)

EVIDENCE_REPO_URL = "https://git.allegedly.works/augur-evidence/augur-evidence.git"


class _EgressTrust(pygit2.RemoteCallbacks):
    """Clone callbacks that accept the cluster mitmproxy's MITM certificate.

    In the claude-sandbox the only egress is the cluster mitmproxy, which
    presents its own CA. libgit2 ignores the OpenSSL ``SSL_CERT_FILE`` the
    Kyverno ``inject-mitmproxy`` policy sets (pointing ``pygit2.settings``
    at the mounted CA does not make libgit2 trust it), so its own validation
    reports the cert invalid and the clone fails with ``user rejected
    certificate``. The connection still goes through the trusted in-cluster
    proxy, so accept an otherwise-invalid cert — but only when an egress proxy
    is actually configured, so normal cert validation still applies elsewhere.
    """

    def certificate_check(self, certificate: object, valid: bool, host: bytes) -> bool:
        return valid or bool(os.environ.get("HTTPS_PROXY"))


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
    callbacks = _EgressTrust(credentials=pygit2.UserPass(username, password))
    pygit2.clone_repository(EVIDENCE_REPO_URL, str(checkout), depth=1, callbacks=callbacks)
    return checkout
