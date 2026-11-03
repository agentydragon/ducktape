from pathlib import Path

import pytest
import pytest_bazel

from agentplane.firecracker.credentials import Credentials, read_credentials


def test_projected_secret_generation_stays_consistent_during_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for version in ("old", "new"):
        directory = tmp_path / version
        directory.mkdir()
        for name in ("tls.crt", "tls.key", "ca.crt"):
            (directory / name).write_text(f"{version}-{name}")
    (tmp_path / "..data").symlink_to("old")
    original_read = Path.read_text

    def read_and_flip(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        value = original_read(path, encoding=encoding, errors=errors)
        if path == tmp_path / "old/tls.crt":
            replacement = tmp_path / "..data-next"
            replacement.symlink_to("new")
            replacement.replace(tmp_path / "..data")
        return value

    monkeypatch.setattr(Path, "read_text", read_and_flip)
    assert read_credentials(tmp_path) == Credentials(
        tls_cert="old-tls.crt", tls_key="old-tls.key", ca_bundle="old-ca.crt"
    )


if __name__ == "__main__":
    pytest_bazel.main()
