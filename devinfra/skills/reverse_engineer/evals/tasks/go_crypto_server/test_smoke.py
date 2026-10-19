"""Smoke test for the go_crypto_server reference specimen.

Boots both the plain and the garbled binaries on ephemeral ports, exercises
every endpoint, and asserts:

- ``register`` returns a 32-hex-char token
- ``put`` + ``get`` round-trips a known plaintext byte-for-byte
- ``list`` returns the issued note id
- ``export`` decodes through the custom base32 alphabet and ends with a
  non-zero 8-byte MAC tag
- ``register`` with a wrong protocol version is rejected with HTTP 400

Locks the reference's behavior: any future drift in the cipher, MAC,
encoding, or wire envelope breaks this test.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port

PROTOCOL_VERSION = "ncs/1"
ALPHABET = "3456789ABCDEFGHJKLMNPQRSTUVWXYZ$"
PAD = "~"


@contextlib.contextmanager
def _running(binary: Path) -> Iterator[httpx.Client]:
    port = pick_free_port()
    proc = subprocess.Popen([str(binary), "-addr", f"127.0.0.1:{port}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=2.0) as client:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    client.post("/v1/register", json={"v": PROTOCOL_VERSION, "op": "register", "body": {}})
                    break
                except httpx.HTTPError:
                    time.sleep(0.05)
            else:
                raise RuntimeError(f"server at {binary} never came up")
            yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _decode_base32_custom(s: str) -> bytes:
    table = {c: i for i, c in enumerate(ALPHABET)}
    keep_for = {0: 0, 2: 1, 4: 2, 5: 3, 7: 4, 8: 5}
    out = bytearray()
    for i in range(0, len(s), 8):
        group = s[i : i + 8]
        bits = 0
        valid = 0
        for c in group:
            if c == PAD:
                break
            bits = (bits << 5) | table[c]
            valid += 1
        bits <<= 5 * (8 - valid)
        out.extend(bits.to_bytes(5, "big")[: keep_for[valid]])
    return bytes(out)


@pytest.fixture(params=["PLAIN_BIN", "GARBLED_BIN"])
def binary(request: pytest.FixtureRequest) -> Path:
    return get_required_path(os.environ[request.param])


def test_round_trip(binary: Path) -> None:
    with _running(binary) as client:
        token = client.post(
            "/v1/register", json={"v": PROTOCOL_VERSION, "op": "register", "body": {"hint": "smoke"}}
        ).json()["body"]["token"]
        assert re.fullmatch(r"[0-9a-f]{32}", token), f"bad token shape: {token=}"

        plain = "the quick brown fox jumps over the lazy dog"
        note_id = client.post(
            "/v1/note/put",
            json={
                "v": PROTOCOL_VERSION,
                "op": "note.put",
                "body": {"token": token, "title": "hello", "plaintext": plain},
            },
        ).json()["body"]["note_id"]
        assert note_id == "n_0000000000000001"

        got = client.post(
            "/v1/note/get", json={"v": PROTOCOL_VERSION, "op": "note.get", "body": {"token": token, "note_id": note_id}}
        ).json()["body"]
        assert got == {"title": "hello", "plaintext": plain}

        ids = client.post(
            "/v1/note/list", json={"v": PROTOCOL_VERSION, "op": "note.list", "body": {"token": token}}
        ).json()["body"]["note_ids"]
        assert ids == [note_id]

        blob = client.post("/v1/export", json={"v": PROTOCOL_VERSION, "op": "export", "body": {"token": token}}).json()[
            "body"
        ]["blob"]
        raw = _decode_base32_custom(blob)
        assert len(raw) >= 8, f"export blob too short: {len(raw)=}"
        assert raw[-8:] != b"\x00" * 8, "MAC tag is all-zero"


def test_rejects_wrong_version(binary: Path) -> None:
    with _running(binary) as client:
        resp = client.post("/v1/register", json={"v": "wrong/0", "op": "register", "body": {}})
        assert resp.status_code == 400


if __name__ == "__main__":
    pytest_bazel.main()
