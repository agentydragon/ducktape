"""Multi-device sync — the failure modes the user reported.

Both "devices" here are `pycrdt.Casino` instances driving the same
FastAPI backend through its `/sync` endpoint via httpx. That is exactly
what the React PWA does over the wire (Yjs and pycrdt speak the same
binary update format), so these tests cover the same code path without
needing a browser. The visual layer is covered separately by
`frontend:visual_main_page`.

Scenarios:
1. Long absence does not nuke other-device state.
2. Concurrent disjoint writes from two devices both persist.
3. Offline writes replay on reconnect.
4. Direct overspend is rejected with structured 409.
5. Server 5xx surfaces — caller never silently keeps a stale view.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pycrdt import Doc, Map

from util.net import pick_free_port
from x.auragon_study_casino.app import create_app
from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.doc_shape import Casino


@pytest.fixture
def casino_server(tmp_path: Path) -> Iterator[str]:
    settings = Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "no-frontend")
    app = create_app(settings)
    port = pick_free_port("127.0.0.1")
    cfg = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, name="casino-uvicorn", daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("backend did not start within 10s")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=5.0)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


class FakeDevice:
    """Wraps a pycrdt Casino + the wire protocol so a test reads like
    two real clients pushing/pulling. `last_server_sv` stays None until
    the device has talked to the server at least once.

    The device starts as a *blank* Doc with the typed handles declared
    but no values written — that mirrors what the production frontend
    does on first boot: declare schema, then bootstrap from the server's
    update before any local writes. Calling `Casino.empty()` here would
    pre-write `credits=0`/`tokens=0` on the client and overwrite the
    server's values via last-write-wins on the next sync, which is
    exactly the bug class these tests are guarding against."""

    def __init__(self, base_url: str, http: httpx.Client) -> None:
        self.base_url = base_url
        self.http = http
        self.casino = Casino(Doc())
        self.last_server_sv: bytes | None = None
        self.last_rejection: dict[str, Any] | None = None

    def sync(self) -> httpx.Response:
        sv = self.last_server_sv if self.last_server_sv is not None else b""
        update = self.casino.get_update(sv)
        r = self.http.post(f"{self.base_url}/sync", json={"state_vector_b64": _b64(sv), "update_b64": _b64(update)})
        if r.status_code == 409:
            self.last_rejection = r.json().get("rejection")
            return r
        if r.status_code != 200:
            return r
        body = r.json()
        server_update = _unb64(body["update_b64"])
        if server_update:
            self.casino.apply_update(server_update)
        self.last_server_sv = _unb64(body["state_vector_b64"])
        self.last_rejection = None
        return r


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=5.0) as c:
        yield c


def test_long_absence_does_not_nuke_other_device_state(casino_server: str, http: httpx.Client) -> None:
    """Phone accumulates a week of state; laptop comes back, *pulls first*,
    and sees everything. Then any local edit on the laptop merges with the
    server without dropping phone's prior writes.

    Both devices bootstrap (sync once) before writing. That mirrors the
    production frontend, which only enables the mutation UI after the
    initial /sync round-trip has completed; without the bootstrap, a
    local write would be CRDT-concurrent with the server's seed values
    and Yjs's clientID-tiebreaker would non-deterministically clobber
    one side."""
    phone = FakeDevice(casino_server, http)
    assert phone.sync().status_code == 200  # bootstrap

    phone.casino.balance["credits"] = 142
    phone.casino.balance["tokens"] = 88

    for sid, subject, secs in [("s-1", "Biochem", 3600), ("s-2", "Anatomy", 1500)]:
        m: Map = Map()
        phone.casino.sessions[sid] = m
        phone.casino.sessions[sid]["subject"] = subject
        phone.casino.sessions[sid]["seconds"] = secs
        phone.casino.sessions[sid]["ended_at_ms"] = 1_700_000_000_000
    assert phone.sync().status_code == 200

    laptop = FakeDevice(casino_server, http)
    assert laptop.sync().status_code == 200  # bootstrap pull
    assert int(laptop.casino.balance["credits"]) == 142
    assert int(laptop.casino.balance["tokens"]) == 88
    assert len(laptop.casino.sessions) == 2

    laptop.casino.sessions["s-3"] = Map()
    laptop.casino.sessions["s-3"]["subject"] = "Pharmacology"
    laptop.casino.sessions["s-3"]["seconds"] = 2400
    laptop.casino.sessions["s-3"]["ended_at_ms"] = 1_700_000_100_000
    assert laptop.sync().status_code == 200

    assert phone.sync().status_code == 200
    assert len(phone.casino.sessions) == 3
    subjects = sorted(str(phone.casino.sessions[sid]["subject"]) for sid in phone.casino.sessions)
    assert subjects == ["Anatomy", "Biochem", "Pharmacology"]


def test_concurrent_disjoint_writes_both_persist(casino_server: str, http: httpx.Client) -> None:
    phone = FakeDevice(casino_server, http)
    laptop = FakeDevice(casino_server, http)
    phone.sync()
    laptop.sync()

    phone.casino.balance["credits"] = 60

    laptop.casino.sessions["s-1"] = Map()
    laptop.casino.sessions["s-1"]["subject"] = "Anatomy"
    laptop.casino.sessions["s-1"]["seconds"] = 1500
    laptop.casino.sessions["s-1"]["ended_at_ms"] = 1_700_000_000_000

    phone.sync()
    laptop.sync()
    phone.sync()  # phone catches laptop's session
    laptop.sync()  # laptop catches phone's credits

    assert int(phone.casino.balance["credits"]) == 60
    assert "s-1" in phone.casino.sessions
    assert int(laptop.casino.balance["credits"]) == 60
    assert "s-1" in laptop.casino.sessions


def test_offline_writes_replay_on_reconnect(casino_server: str, http: httpx.Client) -> None:
    """`sync()` failing transiently never loses the local Y.Doc state — the
    next call sends every accumulated op as one update."""
    phone = FakeDevice(casino_server, http)
    phone.sync()

    # Pretend the phone is offline by pointing it at a closed port so the
    # POST fails. The doc keeps its writes locally.
    bad_url = "http://127.0.0.1:1"  # almost certainly closed
    offline = FakeDevice(bad_url, http)
    offline.casino = phone.casino  # share the same Y.Doc

    for sid in ["off-1", "off-2"]:
        offline.casino.sessions[sid] = Map()
        offline.casino.sessions[sid]["subject"] = "Biochem"
        offline.casino.sessions[sid]["seconds"] = 600
        offline.casino.sessions[sid]["ended_at_ms"] = 1_700_000_000_000

    with pytest.raises((httpx.ConnectError, httpx.RequestError)):
        offline.sync()

    # Reconnect: the same Doc syncs successfully, both sessions land.
    assert phone.sync().status_code == 200

    laptop = FakeDevice(casino_server, http)
    laptop.sync()
    assert {"off-1", "off-2"}.issubset(set(laptop.casino.sessions))


def test_overspend_is_rejected_with_structured_409(casino_server: str, http: httpx.Client) -> None:
    phone = FakeDevice(casino_server, http)
    phone.sync()
    phone.casino.balance["tokens"] = -10

    r = phone.sync()
    assert r.status_code == 409
    body = r.json()
    assert body["rejection"]["rule"] == "tokens_nonneg"
    assert "must be" in body["rejection"]["message"]


def test_server_5xx_surfaces_no_silent_stale_view(casino_server: str, http: httpx.Client, tmp_path: Path) -> None:
    """Point the device at a 503-returning server (pretend it's the real
    server having a bad time); the call surfaces the failure rather than
    silently letting the local doc drift forward of canonical."""
    # Spin up a tiny FastAPI that always 503s on /sync.

    bad = FastAPI()

    @bad.post("/sync")
    def fail() -> PlainTextResponse:
        return PlainTextResponse("backend exploded", status_code=503)

    port = pick_free_port("127.0.0.1")
    cfg = uvicorn.Config(app=bad, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    bad_server = uvicorn.Server(cfg)
    t = threading.Thread(target=bad_server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not bad_server.started:
        time.sleep(0.05)

    try:
        phone = FakeDevice(f"http://127.0.0.1:{port}", http)
        r = phone.sync()
        assert r.status_code == 503
        # The casino's UI banner reacts to non-2xx by entering `offline`
        # state — the JS-side test for that is the visual one. Here we
        # just confirm the error reaches the caller.
    finally:
        bad_server.should_exit = True
        t.join(timeout=5.0)


if __name__ == "__main__":
    pytest_bazel.main()
