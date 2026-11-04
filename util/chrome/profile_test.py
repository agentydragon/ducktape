import sqlite3
from types import SimpleNamespace

import pytest
import pytest_bazel

from util.chrome.profile import ChromeProfile


def test_cookie_selection_uses_network_database_and_preserves_metadata(tmp_path):
    network = tmp_path / "Network"
    network.mkdir()
    database = network / "Cookies"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', '24')")
        db.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
            "path TEXT, expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
            "samesite INTEGER, top_frame_site_key TEXT)"
        )
        db.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (".example.com", "session", "secret", b"", "/", 0, 1, 1, 2, "https://example.com"),
                (".example.com", "session", "other-partition", b"", "/", 0, 1, 1, 2, "https://other.com"),
                (".other.com", "session", "other-host", b"", "/", 0, 1, 1, 2, ""),
                (".example.com", "other", "other-name", b"", "/", 0, 1, 1, 2, ""),
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

    monkeypatch.setattr("util.chrome.profile.subprocess.run", run)
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
