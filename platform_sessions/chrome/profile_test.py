from types import SimpleNamespace

import pytest
import pytest_bazel

from platform_sessions.chrome.profile import ChromeProfile
from platform_sessions.chrome.testing.cookie_db import CookieRow, create_cookie_db


def test_user_agent_uses_chrome_major_version(tmp_path):
    # gaffer-private's platform-session exporters call ChromeProfile.user_agent.
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\nprintf 'Google Chrome 124.0.6367.60\\n'\n")
    chrome.chmod(0o755)

    assert "Chrome/124.0.0.0" in ChromeProfile.user_agent(str(chrome))


def test_cookie_selection_uses_network_database_and_preserves_metadata(tmp_path):
    network = tmp_path / "Network"
    network.mkdir()
    database = network / "Cookies"
    create_cookie_db(
        database,
        [
            CookieRow(
                host=".example.com", name="session", value="secret", same_site=2, partition="https://example.com"
            ),
            CookieRow(
                host=".example.com", name="session", value="other-partition", same_site=2, partition="https://other.com"
            ),
            CookieRow(host=".other.com", name="session", value="other-host", same_site=2),
            CookieRow(host=".example.com", name="other", value="other-name", same_site=2),
        ],
    )

    cookies = ChromeProfile(tmp_path).cookies(
        hosts=(".example.com",), names=("session",), partitions=("https://example.com",)
    )
    assert len(cookies) == 1
    assert cookies[0].plaintext() == "secret"
    assert cookies[0].partition == "https://example.com"
    assert cookies[0].http_only
    assert cookies[0].same_site == 2
    assert not cookies[0].is_expired(1_000_000)
    assert "secret" not in repr(cookies[0])


def test_empty_cookie_selector_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="selectors"):
        ChromeProfile(tmp_path).cookies(hosts=(), names=("session",), partitions=("",))


def test_local_storage_passes_only_selectors_to_helper(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=b'{"auth":"secret"}')

    monkeypatch.setattr("platform_sessions.chrome.profile.subprocess.run", run)
    result = ChromeProfile(tmp_path).local_storage(
        extractor=tmp_path / "helper", origin="https://example.com", keys=("auth",), prefixes=("legacy:",)
    )
    assert result == {"auth": "secret"}
    assert calls[0][0] == [
        str(tmp_path / "helper"),
        str(tmp_path / "Local Storage" / "leveldb"),
        "https://example.com",
        "--key",
        "auth",
        "--prefix",
        "legacy:",
    ]


if __name__ == "__main__":
    pytest_bazel.main()
