from pathlib import Path

import pytest
import pytest_bazel

from aiquota.providers.zai import ZaiSettings, _QuotaResponse, _resolve_api_key, _to_success


@pytest.mark.parametrize(
    ("file_contents", "missing_file", "env_key", "expected"),
    [
        pytest.param("file-key\n", False, None, "file-key", id="configured-file"),
        pytest.param(None, False, "env-key", "env-key", id="environment-fallback"),
        pytest.param("file-key", False, "env-key", "file-key", id="file-takes-precedence"),
        pytest.param("   \n", False, "env-key", "env-key", id="blank-file-falls-back"),
        pytest.param(None, True, "env-key", "env-key", id="missing-file-falls-back"),
        pytest.param(None, False, None, None, id="no-source"),
    ],
)
def test_resolve_api_key(tmp_path, monkeypatch, file_contents, missing_file, env_key, expected):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    if env_key is not None:
        monkeypatch.setenv("ZAI_API_KEY", env_key)

    api_key_path = None
    if missing_file:
        api_key_path = tmp_path / "missing"
    elif file_contents is not None:
        api_key_path = tmp_path / "key"
        api_key_path.write_text(file_contents)

    assert _resolve_api_key(ZaiSettings(api_key_path=api_key_path)) == expected


def test_raw_quota_fixture_preserves_provider_windows() -> None:
    quota = _QuotaResponse.model_validate_json((Path(__file__).parent / "fixtures" / "zai_quota.json").read_text())

    result = _to_success(quota)

    assert [(window.window_seconds, window.used_percent) for window in result.windows] == [
        (5 * 3600, 0),
        (7 * 86400, 9),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
