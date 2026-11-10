"""Smoke test for the go_crypto_server reference specimen.

Boots both the plain and the garbled binaries on ephemeral ports, exercises
the HTTP protocol and export verification behavior, and asserts:

- ``register`` returns a 32-hex-char token
- ``put`` + ``get`` round-trips a known plaintext byte-for-byte
- ``list`` returns the issued note id
- ``export`` decodes through the custom base32 alphabet and ends with a
  non-zero 8-byte MAC tag
- ``export.verify`` accepts empty and populated exports across every encoded
  final-group padding size, and rejects altered tags, ciphertext, session
  keys, short blobs, and malformed base32 input
- ``register`` with a wrong protocol version is rejected with HTTP 400

These are binary-level HTTP checks of the exercised behavior, not standalone
known-answer vectors for every cipher or MAC detail.
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
from python.runfiles import Runfiles

from util.net import listen_tcp

PROTOCOL_VERSION = "ncs/1"
ALPHABET = "3456789ABCDEFGHJKLMNPQRSTUVWXYZ$"
PAD = "~"


@contextlib.contextmanager
def _running(binary: Path) -> Iterator[httpx.Client]:
    with listen_tcp() as listener:
        proc = subprocess.Popen(
            [str(binary), "-addr", f"fd:{listener.socket.fileno()}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=[listener.socket.fileno()],
        )
        listener.socket.close()
        base = f"http://{listener.host}:{listener.port}"
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
            if proc.poll() is None:
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


def _encode_base32_custom(raw: bytes | bytearray) -> str:
    keep_for = {1: 2, 2: 4, 3: 5, 4: 7, 5: 8}
    out: list[str] = []
    for i in range(0, len(raw), 5):
        chunk = raw[i : i + 5]
        bits = int.from_bytes(chunk, "big") << (8 * (5 - len(chunk)))
        encoded = "".join(ALPHABET[(bits >> shift) & 0x1F] for shift in range(35, -1, -5))
        chars = keep_for[len(chunk)]
        out.append(encoded[:chars] + PAD * (8 - chars))
    return "".join(out)


def _register(client: httpx.Client) -> str:
    response = client.post("/v1/register", json={"v": PROTOCOL_VERSION, "op": "register", "body": {}})
    response.raise_for_status()
    token = response.json()["body"]["token"]
    assert isinstance(token, str)
    return token


def _export(client: httpx.Client, token: str) -> str:
    response = client.post("/v1/export", json={"v": PROTOCOL_VERSION, "op": "export", "body": {"token": token}})
    response.raise_for_status()
    blob = response.json()["body"]["blob"]
    assert isinstance(blob, str)
    return blob


def _verify(client: httpx.Client, token: str, blob: str) -> httpx.Response:
    return client.post(
        "/v1/export/verify", json={"v": PROTOCOL_VERSION, "op": "export.verify", "body": {"token": token, "blob": blob}}
    )


def _put(client: httpx.Client, token: str, *, title: str, plaintext: str) -> str:
    response = client.post(
        "/v1/note/put",
        json={
            "v": PROTOCOL_VERSION,
            "op": "note.put",
            "body": {"token": token, "title": title, "plaintext": plaintext},
        },
    )
    response.raise_for_status()
    note_id = response.json()["body"]["note_id"]
    assert isinstance(note_id, str)
    return note_id


@pytest.fixture(params=["PLAIN_BIN", "GARBLED_BIN"])
def binary(request: pytest.FixtureRequest) -> Path:
    return Runfiles.CreateOrRaise().root() / os.environ[request.param]


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


def test_export_verify_accepts_exports_and_padding_sizes(binary: Path) -> None:
    with _running(binary) as client:
        token = _register(client)
        empty_export = _export(client, token)
        assert _verify(client, token, empty_export).json()["body"] == {"valid": True}

        _put(client, token, title="first", plaintext="")
        _put(client, token, title="second", plaintext="several blocks of text")
        listed = client.post(
            "/v1/note/list", json={"v": PROTOCOL_VERSION, "op": "note.list", "body": {"token": token}}
        ).json()["body"]["note_ids"]
        multiple_export = _export(client, token)
        assert _verify(client, token, multiple_export).json()["body"] == {"valid": True}
        assert (
            client.post(
                "/v1/note/list", json={"v": PROTOCOL_VERSION, "op": "note.list", "body": {"token": token}}
            ).json()["body"]["note_ids"]
            == listed
        )

        expected_padding = {0: 0, 1: 6, 2: 4, 3: 3, 4: 1}
        for title_len, pad_count in expected_padding.items():
            padding_token = _register(client)
            _put(client, padding_token, title="x" * title_len, plaintext="")
            blob = _export(client, padding_token)
            assert len(blob) % 8 == 0
            assert len(blob) - len(blob.rstrip(PAD)) == pad_count
            assert _verify(client, padding_token, blob).json()["body"] == {"valid": True}


def test_export_verify_rejects_bad_mac_and_other_session_key(binary: Path) -> None:
    with _running(binary) as client:
        token = _register(client)
        _put(client, token, title="ciphertext", plaintext="secret")
        blob = _export(client, token)
        raw = bytearray(_decode_base32_custom(blob))

        bad_tag = bytearray(raw)
        bad_tag[-1] ^= 1
        response = _verify(client, token, _encode_base32_custom(bad_tag))
        assert response.status_code == 400
        assert response.json()["code"] == 4005

        bad_ciphertext = bytearray(raw)
        note_id_len = bad_ciphertext[0]
        title_len_offset = 1 + note_id_len
        ciphertext_len_offset = title_len_offset + 1 + bad_ciphertext[title_len_offset]
        ciphertext_offset = ciphertext_len_offset + 4
        bad_ciphertext[ciphertext_offset] ^= 1
        response = _verify(client, token, _encode_base32_custom(bad_ciphertext))
        assert response.status_code == 400
        assert response.json()["code"] == 4005

        other_token = _register(client)
        response = _verify(client, other_token, blob)
        assert response.status_code == 400
        assert response.json()["code"] == 4005


def test_export_verify_rejects_unknown_token_and_malformed_encoding(binary: Path) -> None:
    with _running(binary) as client:
        token = _register(client)
        blob = _export(client, token)

        response = _verify(client, "00" * 16, blob)
        assert response.status_code == 400
        assert response.json()["code"] == 4002

        malformed_blobs = [
            "",  # decodes successfully, but cannot contain the 8-byte tag
            _encode_base32_custom(b"\x00" * 7),  # valid encoding, but still shorter than the MAC tag
            "333",  # short, non-multiple-of-8 group
            "0" + blob[1:],  # glyph outside the custom alphabet
            "333~~~~~",  # unsupported group length and padding count
            "33~~~~~~" + blob[8:],  # padding is only legal in the final group
            "33~33333" + blob[8:],  # data cannot follow padding within a group
        ]
        final_data_offset = len(blob.rstrip(PAD)) - 1
        final_data_value = ALPHABET.index(blob[final_data_offset])
        assert final_data_value % 2 == 0
        malformed_blobs.append(
            blob[:final_data_offset] + ALPHABET[final_data_value | 1] + blob[final_data_offset + 1 :]
        )
        for malformed in malformed_blobs:
            response = _verify(client, token, malformed)
            assert response.status_code == 400
            assert response.json()["code"] == 4001

        # Malformed requests must be handled as client errors without stopping the server.
        assert _verify(client, token, blob).json()["body"] == {"valid": True}


if __name__ == "__main__":
    pytest_bazel.main()
