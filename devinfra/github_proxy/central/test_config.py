from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.github_proxy.central.config import ClientCredential, Settings


def test_secret_errors_do_not_disclose_passwords() -> None:
    with pytest.raises(ValidationError) as caught:
        ClientCredential.model_validate(
            {"username": "test-private-invalid-client/id", "password": "test-private-password"}
        )
    assert "test-private-password" not in str(caught.value)
    with pytest.raises(ValidationError):
        ClientCredential.model_validate({"username": "test-client", "password": ""})


def test_separate_secret_files_reject_duplicate_clients(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path, password in ((first, "test-private-first"), (second, "test-private-second")):
        path.mkdir()
        (path / "username").write_text("test-client")
        (path / "password").write_text(password)
    settings = Settings(
        proxy_hostname="proxy.test",
        credential_dirs=[first, second],
        proxy_tls_cert_file=tmp_path / "outer.crt",
        proxy_tls_key_file=tmp_path / "outer.key",
        interception_ca_cert_file=tmp_path / "ca.crt",
        interception_ca_key_file=tmp_path / "ca.key",
        confdir=tmp_path / "conf",
        capture_path=tmp_path / "capture.flows",
        session_ws_events=tmp_path / "ws.jsonl",
    )
    with pytest.raises(ValueError, match="Duplicate client IDs"):
        settings.credentials()


if __name__ == "__main__":
    pytest_bazel.main()
