"""Tests for SOPS YAML decryption via pyrage.

Uses testdata files encrypted by the real sops CLI (not synthetic encryption),
so the tests validate against the actual SOPS format.
"""

from pathlib import Path

import pytest
import pytest_bazel
from pyrage import x25519

from devinfra.claude.sops_decrypt import _parse_enc_value, decrypt_sops_yaml, load_age_identities
from util.bazel.runfiles import get_required_path

# Committed testdata encrypted by `sops --encrypt --age <recipient>`.
_TESTDATA_YAML = "_main/devinfra/claude/testdata/sops_test_secrets.yaml"
_TESTDATA_AGE_KEY = "_main/devinfra/claude/testdata/sops_test_age_key.txt"

_EXPECTED = {"api_key": "test-secret-value", "another_key": "another-secret"}


@pytest.fixture
def sops_file() -> Path:
    return get_required_path(_TESTDATA_YAML)


@pytest.fixture
def test_identities() -> list:
    key_text = get_required_path(_TESTDATA_AGE_KEY).read_text()
    return load_age_identities(key_text)


def test_parse_enc_value():
    enc = "ENC[AES256_GCM,data:dGVzdA==,iv:AAAAAAAAAAAAAAAA,tag:AAAAAAAAAAAAAAAAAAAAAA==,type:str]"
    ct_tag, iv, value_type = _parse_enc_value(enc)
    assert iv == b"\x00" * 12
    assert value_type == b"str"
    assert len(ct_tag) > 0


def test_parse_enc_value_invalid():
    with pytest.raises(ValueError, match="Not a valid SOPS"):
        _parse_enc_value("not-encrypted")


def test_decrypt_sops_yaml(sops_file: Path, test_identities: list):
    """Decrypt a file encrypted by the real sops CLI."""
    result = decrypt_sops_yaml(sops_file, test_identities)
    assert result == _EXPECTED


def test_decrypt_sops_yaml_no_sops_metadata(tmp_path: Path, test_identities: list):
    path = tmp_path / "plain.yaml"
    path.write_text("key: value\n")
    with pytest.raises(ValueError, match="No 'sops' metadata"):
        decrypt_sops_yaml(path, test_identities)


def test_decrypt_sops_yaml_wrong_identity(sops_file: Path):
    wrong_identity = x25519.Identity.generate()
    with pytest.raises(ValueError, match="Could not decrypt SOPS data key"):
        decrypt_sops_yaml(sops_file, [wrong_identity])


def test_load_age_identities():
    key_text = get_required_path(_TESTDATA_AGE_KEY).read_text()
    identities = load_age_identities(key_text)
    assert len(identities) == 1


def test_load_age_identities_with_comments():
    key_text = "# created: 2026-03-28\n# public key: ...\nAGE-SECRET-KEY-1VL0XPAP3L02ZMAKXWDF0WQLGYCTYUQF77N4FDLHSCZJVMMW7TH4QS2APFM\n"
    identities = load_age_identities(key_text)
    assert len(identities) == 1


def test_load_age_identities_empty():
    with pytest.raises(ValueError, match="No AGE-SECRET-KEY"):
        load_age_identities("# just a comment\n")


def test_roundtrip_with_env_var_key(sops_file: Path):
    """End-to-end: load identity from key file text, decrypt real sops file."""
    key_text = get_required_path(_TESTDATA_AGE_KEY).read_text()
    identities = load_age_identities(key_text)
    result = decrypt_sops_yaml(sops_file, identities)
    assert result["api_key"] == "test-secret-value"


if __name__ == "__main__":
    pytest_bazel.main()
