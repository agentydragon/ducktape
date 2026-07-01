import pytest_bazel

from aiquota.providers.zai import ZaiSettings, _resolve_api_key


def test_resolve_api_key_from_file(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("file-key\n")
    assert _resolve_api_key(ZaiSettings(api_key_path=key_file)) == "file-key"


def test_resolve_api_key_env_fallback(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    assert _resolve_api_key(ZaiSettings(api_key_path=None)) == "env-key"


def test_resolve_api_key_file_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    key_file = tmp_path / "key"
    key_file.write_text("file-key")
    assert _resolve_api_key(ZaiSettings(api_key_path=key_file)) == "file-key"


def test_resolve_api_key_empty_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    key_file = tmp_path / "key"
    key_file.write_text("   \n")
    assert _resolve_api_key(ZaiSettings(api_key_path=key_file)) == "env-key"


def test_resolve_api_key_missing_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    assert _resolve_api_key(ZaiSettings(api_key_path=tmp_path / "missing")) == "env-key"


def test_resolve_api_key_none_when_no_source(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert _resolve_api_key(ZaiSettings(api_key_path=None)) is None


if __name__ == "__main__":
    pytest_bazel.main()
