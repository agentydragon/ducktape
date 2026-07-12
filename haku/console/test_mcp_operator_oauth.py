"""Unit tests for the OAuth agent→operator subject extraction (`operator_subject_from_idp_tokens`).

The extracted value must be the id_token `sub` — the same opaque key the operator browser session
and the `mcp_operator_oauth_associations` rows use (both console providers run `sub_mode=user_id`).
"""

from __future__ import annotations

import jwt
import pytest_bazel

from haku.console.mcp_operator_oauth import operator_subject_from_idp_tokens


def _id_token(claims: dict[str, object]) -> str:
    # operator_subject_from_idp_tokens decodes with verify_signature=False, so any key/alg works here.
    return jwt.encode(claims, "unused-signing-key", algorithm="HS256")


def test_extracts_sub_not_username() -> None:
    idp = {"id_token": _id_token({"sub": "42", "preferred_username": "agentydragon", "email": "a@b.c"})}
    assert operator_subject_from_idp_tokens(idp) == "42"


def test_none_without_id_token_or_sub() -> None:
    assert operator_subject_from_idp_tokens({"access_token": "opaque"}) is None
    # A username but no `sub` is not enough — the link keys on the opaque subject only.
    assert operator_subject_from_idp_tokens({"id_token": _id_token({"preferred_username": "agentydragon"})}) is None


if __name__ == "__main__":
    pytest_bazel.main()
