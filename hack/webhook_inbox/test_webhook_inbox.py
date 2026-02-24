from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import webhook_inbox
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


@pytest.fixture
def app_and_client(tmp_path: Path):
    """Return (app_module, client) wired to test database."""

    webhook_inbox.configure_db(tmp_path / "test.db")

    client = TestClient(webhook_inbox.app)
    try:
        yield webhook_inbox, client
    finally:
        client.close()


@pytest.fixture
def client(app_and_client):
    _, client = app_and_client
    return client


# Ingest endpoint
# ---------------------------------------------------------------------------


def test_ingest_persists_event(app_and_client):
    app, client = app_and_client

    payload = "Hello, webhook!"
    resp = client.post("/", data=payload)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    row = app.CONN.execute("SELECT payload FROM events").fetchone()
    assert row
    assert row[0] == payload


def test_payload_too_large(app_and_client):
    app, client = app_and_client
    oversize = "x" * (app.MAX_PAYLOAD + 1)
    assert client.post("/", data=oversize).status_code == 413


def test_invalid_utf8_payload(client):
    garbage = b"\x80\x80"  # invalid UTF-8
    resp = client.post("/", data=garbage)
    assert resp.status_code == 400


# Paging parameters
# ---------------------------------------------------------------------------


def test_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?before=")


def test_bad_before(client):
    assert client.get("/?before=not_an_int").status_code == 400


def test_missing_count_redirects_to_default(app_and_client):
    app, client = app_and_client

    ts = 1111111111
    r = client.get(f"/?before={ts}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert f"before={ts}" in loc
    assert f"count={app.PAGE_SIZE}" in loc


@pytest.mark.parametrize("bad", [0, -1])
def test_too_low_counts_raise_400(client, bad):
    assert client.get(f"/?before=123&count={bad}").status_code == 400


def test_too_high_count_raises_400(app_and_client):
    app, client = app_and_client
    assert client.get(f"/?before=123&count={app.PAGE_SIZE + 1}").status_code == 400


def test_smaller_count_is_accepted(client):
    assert client.get("/?before=123&count=1").status_code == 200


# Encryption / decryption round-trip -----------------------------------------
def test_crypto_roundtrip():
    key = Fernet.generate_key().decode()
    events = [{"id": 1, "ts": 42, "payload": "x"}]
    ciphertext = webhook_inbox.encrypt_events(events, key)

    ns: dict[str, object] = {"KEY": key, "CIPHERTEXT": ciphertext}

    # Snippet should define decrypt_events() *and* assign `events`.
    exec(webhook_inbox.DECRYPT_CODE_SNIPPET, ns)

    assert ns["events"] == events


if __name__ == "__main__":
    pytest_bazel.main()
