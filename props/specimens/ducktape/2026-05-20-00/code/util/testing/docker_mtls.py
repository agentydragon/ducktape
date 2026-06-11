"""pytest fixture for Docker mTLS cert assembly.

Reads DUCKTAPE_DOCKER_CLIENT_KEY (base64-encoded PEM) from the environment and
atomically sets DOCKER_HOST, DOCKER_TLS_VERIFY, and DOCKER_CERT_PATH so that
docker.from_env() / aiodocker.Docker() pick up mTLS automatically.

No-op when DUCKTAPE_DOCKER_CLIENT_KEY is not set (falls back to default Docker).

TODO: Once docker-ci is live, .envrc/web_setup.sh will pass the PEM via
BBR_REMOTE_ARGS using secret-env-overrides-base64. That means bb remote handles
the base64 encoding — this fixture should then read the raw PEM directly from the
env var instead of base64-decoding it.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
from pathlib import Path

import pytest

from util.bazel.runfiles import get_required_path

_RLOCATION_CA = "_main/cluster/k8s/docker-ci/certs/ca.pem"
_RLOCATION_CLIENT_CERT = "_main/cluster/k8s/docker-ci/certs/client-cert.pem"

_DOCKER_HOST = "tcp://docker-ci.allegedly.works:2376"


@pytest.fixture(autouse=True)
def docker_mtls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assemble Docker mTLS cert dir and set all Docker env vars atomically."""
    client_key_b64 = os.environ.get("DUCKTAPE_DOCKER_CLIENT_KEY")
    if not client_key_b64:
        return

    client_key = base64.b64decode(client_key_b64).decode()

    cert_dir = tmp_path / "docker-certs"
    cert_dir.mkdir()

    try:
        ca_path = get_required_path(_RLOCATION_CA)
        cert_path = get_required_path(_RLOCATION_CLIENT_CERT)
    except RuntimeError:
        pytest.skip("Docker mTLS certs not in runfiles")

    # Docker expects exactly: ca.pem, cert.pem, key.pem
    shutil.copy(ca_path, cert_dir / "ca.pem")
    shutil.copy(cert_path, cert_dir / "cert.pem")

    key_file = cert_dir / "key.pem"
    key_file.write_text(client_key)
    key_file.chmod(stat.S_IRUSR)

    monkeypatch.setenv("DOCKER_CERT_PATH", str(cert_dir))
    monkeypatch.setenv("DOCKER_HOST", _DOCKER_HOST)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
