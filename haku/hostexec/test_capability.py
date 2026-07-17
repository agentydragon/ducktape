"""Tests for the console-signed exec capability.

Covers the semantic properties `hostexecd` relies on (sign/verify, tamper/expiry/argv/run_as
rejection, wrong-key rejection) and pins the byte-level encoding so the Rust verifier has a
fixed cross-language contract to reproduce. The signing/argv encodings are the only things that
can drift between Python (signer) and Rust (verifier); Ed25519 over identical bytes is
guaranteed identical by RFC 8032, so pinning the encoding is sufficient.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.hostexec.capability import (
    ArgvMismatchError,
    BadSignatureError,
    CapabilityClaims,
    ExpiredError,
    RunAs,
    RunAsMismatchError,
    SignedCapability,
    argv_digest,
    check_capability,
    generate_keypair,
    load_private_key,
    load_public_key,
    sign_capability,
    verify_signature,
)

# Pinned cross-language vectors (computed from the canonical encoding; see the Rust verifier).
_NONCE = bytes.fromhex("00112233445566778899aabbccddeeff")
_VECTOR_ARGV = ["bash", "-lc", "echo hello"]
_VECTOR_ARGV_DIGEST_HEX = "313a90f4f96bbd6549b841e315dd09d6fea8014387e2b0cd30225b3beb04b9bc"
_VECTOR_SIGNING_MESSAGE_HEX = (
    "6475636b746170652e686f7374657865632e6361706162696c6974792e7631000000057779726d32"
    "00000004726f6f7400000020313a90f4f96bbd6549b841e315dd09d6fea8014387e2b0cd30225b3b"
    "eb04b9bc01000000122f686f6d652f6167656e7479647261676f6e0000001000112233445566778899"
    "aabbccddeeff0000000070dbd880"
)


def _claims(*, host="wyrm2", run_as=RunAs.ROOT, argv=_VECTOR_ARGV, cwd="/home/agentydragon", exp=1893456000):
    return CapabilityClaims(host=host, run_as=run_as, argv_sha256=argv_digest(argv), cwd=cwd, nonce=_NONCE, exp=exp)


def test_argv_digest_pins_encoding():
    assert argv_digest(_VECTOR_ARGV).hex() == _VECTOR_ARGV_DIGEST_HEX


def test_signing_message_pins_encoding():
    assert _claims().signing_message().hex() == _VECTOR_SIGNING_MESSAGE_HEX


def test_argv_digest_no_delimiter_injection():
    # Length-prefixed framing: ["ab","c"] and ["a","bc"] must not collide.
    assert argv_digest(["ab", "c"]) != argv_digest(["a", "bc"])
    assert argv_digest(["x"]) != argv_digest(["x", ""])


def test_signing_message_distinguishes_absent_and_empty_cwd():
    assert _claims(cwd=None).signing_message() != _claims(cwd="").signing_message()


def test_sign_verify_roundtrip():
    seed, pub = generate_keypair()
    claims = _claims()
    sig = sign_capability(claims, load_private_key(seed))
    verify_signature(claims, sig, load_public_key(pub))  # does not raise


def test_ed25519_is_deterministic():
    seed, _ = generate_keypair()
    sk = load_private_key(seed)
    claims = _claims()
    assert sign_capability(claims, sk) == sign_capability(claims, sk)


def test_tampered_field_fails_signature():
    seed, pub = generate_keypair()
    sig = sign_capability(_claims(), load_private_key(seed))
    with pytest.raises(BadSignatureError):
        verify_signature(_claims(host="rugged"), sig, load_public_key(pub))


def test_wrong_public_key_fails():
    seed, _ = generate_keypair()
    _, other_pub = generate_keypair()
    claims = _claims()
    sig = sign_capability(claims, load_private_key(seed))
    with pytest.raises(BadSignatureError):
        verify_signature(claims, sig, load_public_key(other_pub))


def test_check_capability_accepts_matching_request():
    seed, pub = generate_keypair()
    claims = _claims()
    sig = sign_capability(claims, load_private_key(seed))
    check_capability(
        claims,
        sig,
        public_key=load_public_key(pub),
        host="wyrm2",
        run_as=RunAs.ROOT,
        argv=_VECTOR_ARGV,
        now=claims.exp - 1,
    )


def test_check_capability_rejects_argv_swap():
    seed, pub = generate_keypair()
    claims = _claims()
    sig = sign_capability(claims, load_private_key(seed))
    with pytest.raises(ArgvMismatchError):
        check_capability(
            claims,
            sig,
            public_key=load_public_key(pub),
            host="wyrm2",
            run_as=RunAs.ROOT,
            argv=["rm", "-rf", "/"],
            now=claims.exp - 1,
        )


def test_check_capability_rejects_run_as_swap():
    seed, pub = generate_keypair()
    claims = _claims(run_as=RunAs.ROOT)
    sig = sign_capability(claims, load_private_key(seed))
    with pytest.raises(RunAsMismatchError):
        check_capability(
            claims,
            sig,
            public_key=load_public_key(pub),
            host="wyrm2",
            run_as=RunAs.AGENTYDRAGON,
            argv=_VECTOR_ARGV,
            now=claims.exp - 1,
        )


def test_check_capability_rejects_expired():
    seed, pub = generate_keypair()
    claims = _claims()
    sig = sign_capability(claims, load_private_key(seed))
    with pytest.raises(ExpiredError):
        check_capability(
            claims,
            sig,
            public_key=load_public_key(pub),
            host="wyrm2",
            run_as=RunAs.ROOT,
            argv=_VECTOR_ARGV,
            now=claims.exp + 1,
        )


def test_signed_capability_wire_roundtrip():
    seed, pub = generate_keypair()
    claims = _claims()
    wire = SignedCapability.create(claims, load_private_key(seed))
    # Serializes and parses like any Pydantic model.
    reparsed = SignedCapability.model_validate_json(wire.model_dump_json())
    assert reparsed.claims() == claims
    verify_signature(reparsed.claims(), reparsed.signature_bytes(), load_public_key(pub))


if __name__ == "__main__":
    pytest_bazel.main()
