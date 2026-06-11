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


def _trust_egress_ca() -> None:
    """Point libgit2 at the egress CA bundle that the environment already trusts.

    libgit2 does not consult OpenSSL's ``SSL_CERT_FILE`` env var (the way
    ``requests``/``curl`` do), so in a TLS-inspecting egress environment — e.g.
    the Claude agent sandbox, where the Kyverno ``inject-mitmproxy`` policy
    mounts the mitmproxy CA and sets ``SSL_CERT_FILE`` to it — the clone is
    rejected with ``user rejected certificate for git.allegedly.works``. Mirror
    that bundle into libgit2's own trust setting (``GIT_OPT_SET_SSL_CERT_LOCATIONS``).
    A no-op when neither var is set (libgit2 then uses its built-in default).
    """
    if ca_file := os.environ.get("GIT_SSL_CAINFO") or os.environ.get("SSL_CERT_FILE"):
        pygit2.settings.ssl_cert_file = ca_file


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
    _trust_egress_ca()
    checkout.parent.mkdir(parents=True, exist_ok=True)
    callbacks = pygit2.RemoteCallbacks(credentials=pygit2.UserPass(username, password))
    pygit2.clone_repository(EVIDENCE_REPO_URL, str(checkout), depth=1, callbacks=callbacks)
    return checkout
