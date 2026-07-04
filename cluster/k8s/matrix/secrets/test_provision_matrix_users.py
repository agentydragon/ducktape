"""Tests for provision_matrix_users.py's two-phase Matrix/Synapse provisioning."""

import hashlib
import hmac
import io
import json
import urllib.error
import urllib.request

import pytest
import pytest_bazel

from cluster.k8s.matrix.secrets.provision_matrix_users import (
    ADMIN_USERNAME,
    BOT_DISPLAYNAME,
    BOT_USERNAME,
    SERVER_NAME,
    SYNAPSE_URL,
    _bot_exists,
    register_admin,
    upsert_bot,
)

NONCE = "test-nonce"


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="url", code=code, msg="error", hdrs=None, fp=io.BytesIO(json.dumps(body).encode())
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body


class _Recorder:
    """Records every urlopen() call as (method, url, decoded JSON body or None)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def record(self, req: urllib.request.Request) -> tuple[str, str, dict | None]:
        body = json.loads(req.data) if req.data else None
        call = (req.get_method(), req.full_url, body)
        self.calls.append(call)
        return call


def test_register_admin_computes_hmac_and_posts_registration(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder()

    def fake_urlopen(req: urllib.request.Request):
        method, url, _ = recorder.record(req)
        assert url == f"{SYNAPSE_URL}/_synapse/admin/v1/register"
        if method == "GET":
            return _FakeResponse({"nonce": NONCE})
        return _FakeResponse({"user_id": f"@{ADMIN_USERNAME}:{SERVER_NAME}"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    register_admin("shared-secret", "adminpw")

    _, _, register_body = recorder.calls[1]
    assert register_body is not None
    expected_mac = hmac.new(
        b"shared-secret", f"{NONCE}\0{ADMIN_USERNAME}\0adminpw\0admin".encode(), hashlib.sha1
    ).hexdigest()
    assert register_body == {
        "nonce": NONCE,
        "username": ADMIN_USERNAME,
        "password": "adminpw",
        "admin": True,
        "mac": expected_mac,
    }


def test_register_admin_skips_when_already_registered(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def fake_urlopen(req: urllib.request.Request):
        if req.get_method() == "GET":
            return _FakeResponse({"nonce": NONCE})
        raise _http_error(400, {"errcode": "M_USER_IN_USE", "error": "User ID already taken."})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    register_admin("shared-secret", "adminpw")  # must not raise

    assert "already exists" in capsys.readouterr().out


def test_register_admin_raises_on_unexpected_error(monkeypatch: pytest.MonkeyPatch):
    def fake_urlopen(req: urllib.request.Request):
        if req.get_method() == "GET":
            return _FakeResponse({"nonce": NONCE})
        raise _http_error(500, {"errcode": "M_UNKNOWN", "error": "boom"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="Phase 1 failed"):
        register_admin("shared-secret", "adminpw")


def test_bot_exists_true_when_response_has_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req: _FakeResponse({"name": f"@{BOT_USERNAME}:{SERVER_NAME}"})
    )
    assert _bot_exists("token", "encoded-mxid") is True


def test_bot_exists_false_when_response_has_no_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: _FakeResponse({}))
    assert _bot_exists("token", "encoded-mxid") is False


def test_bot_exists_false_on_404(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: (_ for _ in ()).throw(_http_error(404, {})))
    assert _bot_exists("token", "encoded-mxid") is False


def test_bot_exists_reraises_non_404_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: (_ for _ in ()).throw(_http_error(500, {})))
    with pytest.raises(urllib.error.HTTPError):
        _bot_exists("token", "encoded-mxid")


def test_upsert_bot_creates_with_password_when_absent(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder()

    def fake_urlopen(req: urllib.request.Request):
        method, url, _ = recorder.record(req)
        if url == f"{SYNAPSE_URL}/_matrix/client/v3/login":
            return _FakeResponse({"access_token": "admin-token"})
        if method == "GET":
            return _FakeResponse({})  # bot does not exist yet
        return _FakeResponse({"displayname": BOT_DISPLAYNAME})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    upsert_bot("adminpw", "botpw")

    [(_, _, put_body)] = [c for c in recorder.calls if c[0] == "PUT"]
    assert put_body == {"password": "botpw", "displayname": BOT_DISPLAYNAME, "admin": False}


def test_upsert_bot_updates_without_password_when_present(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder()

    def fake_urlopen(req: urllib.request.Request):
        method, url, _ = recorder.record(req)
        if url == f"{SYNAPSE_URL}/_matrix/client/v3/login":
            return _FakeResponse({"access_token": "admin-token"})
        if method == "GET":
            return _FakeResponse({"name": f"@{BOT_USERNAME}:{SERVER_NAME}"})
        return _FakeResponse({"displayname": BOT_DISPLAYNAME})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    upsert_bot("adminpw", "botpw")

    [(_, _, put_body)] = [c for c in recorder.calls if c[0] == "PUT"]
    assert put_body == {"displayname": BOT_DISPLAYNAME, "admin": False}


if __name__ == "__main__":
    pytest_bazel.main()
