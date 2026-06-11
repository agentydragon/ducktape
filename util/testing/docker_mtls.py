"""pytest fixture for Docker mTLS cert assembly (dormant).

Sets DOCKER_HOST / DOCKER_TLS_VERIFY / DOCKER_CERT_PATH from a client key in
DUCKTAPE_DOCKER_CLIENT_KEY so docker.from_env() / aiodocker.Docker() pick up mTLS
to the docker-ci DinD automatically. Loaded into every `requires_docker` test via
`-p util.testing.docker_mtls` (devinfra/python/defs.bzl).

CLEANUP(2026-06-11): This is the external-RBE path for `bbr test` against
docker-ci, and it is NOT wired up — devinfra/secrets/_common.sh never exports
DUCKTAPE_DOCKER_CLIENT_KEY (that block is commented out), so the fixture always
no-ops. The in-cluster eval Job (loom/gym) reaches docker-ci over its own
cert-manager-issued Secret and does not use this fixture.

The docker-ci PKI moved to cert-manager (cluster-internal-ca), so the old in-repo
public certs and the SOPS-encrypted client key this fixture used to read were
deleted. Reviving the path means: issue a clientAuth cert from cluster-internal-ca,
export its cert + key (e.g. from the claude-sandbox `docker-ci-client` Secret) out
to the RBE executors, and re-add the cert-dir assembly here. Delete this fixture
(and the `-p util.testing.docker_mtls` wiring in devinfra/python/defs.bzl) if
external-RBE docker-ci access is abandoned for good.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def docker_mtls() -> None:
    """No-op while the external-RBE docker-ci path is dormant (see module docstring)."""
    if os.environ.get("DUCKTAPE_DOCKER_CLIENT_KEY"):
        raise RuntimeError(
            "DUCKTAPE_DOCKER_CLIENT_KEY is set but the docker_mtls fixture is "
            "dormant: its cert plumbing was removed when docker-ci moved to "
            "cert-manager. See the module docstring to revive it."
        )
