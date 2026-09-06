import json
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from cluster.proxies.github_api_proxy.config import ClientPasswords, Settings


def test_secret_errors_do_not_disclose_passwords() -> None:
    with pytest.raises(ValidationError) as caught:
        ClientPasswords.model_validate({"test-private-invalid-client/id": "test-private-password"})
    assert "test-private-password" not in str(caught.value)
    with pytest.raises(ValidationError):
        ClientPasswords.model_validate({"test-client": ""})


def test_separate_secret_files_reject_duplicate_clients(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"test-client": "test-private-first"}))
    second.write_text(json.dumps({"test-client": "test-private-second"}))
    settings = Settings(
        proxy_hostname="proxy.test",
        credential_files=[first, second],
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
