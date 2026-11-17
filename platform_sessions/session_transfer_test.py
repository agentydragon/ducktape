import json
from typing import cast

import pytest
import pytest_bazel

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.session_transfer import decode_json_request, encode_json


def test_json_roundtrip_and_provider_binding():
    session = {"nested": {"cookies": [1, 2]}, "label": "synthetic"}
    exported = encode_json("example-provider", session)

    assert json.loads(exported) == {"provider": "example-provider", "session": session}
    assert decode_json_request(exported, "example-provider") == session
    assert decode_json_request(exported.encode("utf-8"), "example-provider") == session
    with pytest.raises(ValueError, match="Invalid session export"):
        decode_json_request(exported, "another-provider")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"value": "synthetic"},
        {"provider": "example-provider"},
        {"provider": "another-provider", "session": {}},
        {"provider": "example-provider", "session": "synthetic"},
        {"provider": "example-provider", "session": []},
        {"provider": "example-provider", "session": {}, "extra": True},
        {"version": 1, "provider": "example-provider", "session": {}},
    ],
)
def test_malformed_envelopes_are_rejected(payload):
    with pytest.raises(ValueError, match="Invalid session export"):
        decode_json_request(json.dumps(payload), "example-provider")


@pytest.mark.parametrize("value", ["invalid JSON", "", "old-envelope-format"])
def test_non_json_exports_are_rejected(value):
    with pytest.raises(ValueError, match=r"\S"):
        decode_json_request(value, "example-provider")


def test_export_and_request_byte_bounds():
    overhead = len(encode_json("example-provider", {"data": ""}).encode("utf-8"))
    session = {"data": "x" * (MAX_SESSION_BYTES - overhead)}
    exported = encode_json("example-provider", session)

    assert len(exported.encode("utf-8")) == MAX_SESSION_BYTES
    assert decode_json_request(exported, "example-provider") == session
    with pytest.raises(ValueError, match="exceeds the 65536-byte limit"):
        encode_json("example-provider", {"data": session["data"] + "x"})
    with pytest.raises(ValueError, match="Invalid session export"):
        decode_json_request(exported + " ", "example-provider")
    oversized_unicode = json.dumps(
        {"provider": "example-provider", "session": {"data": "é" * 33000}}, ensure_ascii=False
    )
    with pytest.raises(ValueError, match="Invalid session export"):
        decode_json_request(oversized_unicode, "example-provider")


@pytest.mark.parametrize("provider", ["", None, 1])
def test_export_requires_provider_identity(provider):
    with pytest.raises(ValueError, match="provider is required"):
        encode_json(provider, {})


def test_export_requires_object_session():
    with pytest.raises(ValueError, match="must be an object"):
        encode_json("example-provider", cast(dict, []))


if __name__ == "__main__":
    pytest_bazel.main()
