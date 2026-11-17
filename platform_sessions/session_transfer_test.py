import json
from pathlib import Path
from typing import Any, cast

import pytest
from python.runfiles import runfiles

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.session_transfer import decode_json_request, encode_json


def _session_transfer_examples() -> dict[str, list[dict[str, Any]]]:
    files = runfiles.Create()
    assert files is not None
    path = files.Rlocation("ducktape/platform_sessions/testdata/session_transfer_examples.json")
    assert path is not None
    return cast(dict[str, list[dict[str, Any]]], json.loads(Path(path).read_text()))


def test_valid_envelope_examples():
    for example in _session_transfer_examples()["valid_envelopes"]:
        expected_provider = example["expected_provider"]
        envelope = example["envelope"]
        assert isinstance(expected_provider, str)
        assert isinstance(envelope, dict)
        session = envelope["session"]

        exported = encode_json(expected_provider, session)
        assert json.loads(exported) == envelope, example["name"]
        encoded_envelope = json.dumps(envelope)
        assert decode_json_request(encoded_envelope, expected_provider) == session, example["name"]
        assert decode_json_request(encoded_envelope.encode("utf-8"), expected_provider) == session


def test_invalid_envelope_examples_are_rejected():
    for example in _session_transfer_examples()["invalid_envelopes"]:
        with pytest.raises(ValueError, match="Invalid session export"):
            decode_json_request(json.dumps(example["envelope"]), example["expected_provider"])


def test_non_json_examples_are_rejected():
    for example in _session_transfer_examples()["invalid_json"]:
        with pytest.raises(ValueError, match=r"\S"):
            decode_json_request(example["value"], "example-provider")


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
