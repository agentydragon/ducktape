from pathlib import Path

import pytest_bazel

from aiquota.providers.zai import ZaiSettings, _QuotaResponse, _resolve_api_key, _to_success


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


def test_raw_quota_fixture_preserves_provider_windows() -> None:
    quota = _QuotaResponse.model_validate_json((Path(__file__).parent / "fixtures" / "zai_quota.json").read_text())

    result = _to_success(quota)

    assert [(window.window_seconds, window.used_percent) for window in result.windows] == [
        (5 * 3600, 0),
        (7 * 86400, 9),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
