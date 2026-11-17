"""Bounded JSON envelope for transferring provider-owned session objects."""

import json

from platform_sessions.limits import MAX_SESSION_BYTES


def encode_json(provider: str, session: dict) -> str:
    """Encode a session object tagged for the receiving provider."""
    if not isinstance(provider, str) or not provider:
        raise ValueError("Session export provider is required")
    if not isinstance(session, dict):
        raise ValueError("Session export must be an object")
    value = json.dumps({"provider": provider, "session": session})
    if len(value.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValueError(f"Session export exceeds the {MAX_SESSION_BYTES}-byte limit")
    return value


def decode_json_request(body: bytes | bytearray | str, provider: str) -> dict:
    """Validate the envelope and return its session for the named provider.

    Provider identity is supplied by the caller because this shared codec has no
    provider roster. The caller remains responsible for validating the session.
    """
    if not isinstance(provider, str) or not provider:
        raise ValueError("Invalid session export")
    if not isinstance(body, (bytes, bytearray, str)):
        raise ValueError("Invalid session export")
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    if len(body_bytes) > MAX_SESSION_BYTES:
        raise ValueError("Invalid session export")

    payload = json.loads(body)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"provider", "session"}
        or payload["provider"] != provider
        or not isinstance(payload["session"], dict)
    ):
        raise ValueError("Invalid session export")
    return payload["session"]
