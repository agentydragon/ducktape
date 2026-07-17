"""Tests for the console-signed exec capability (EdDSA JWT).

Covers the semantic properties `hostexecd` relies on: mint/verify roundtrip and rejection of a
wrong key, a tampered token, an expired token, a wrong audience, a missing `exp`, an argv swap,
and a run_as swap. `test_emit_rust_vector` prints a deterministic Python-signed token + public
PEM (run with `--test_output=all`) that the Rust `hostexecd` verifier pins, proving the
cross-language JWT contract with a real Python-minted token.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
import pytest_bazel

from haku.hostexec.capability import (
    CAPABILITY_AUDIENCE,
    ArgvMismatchError,
    CapabilityClaims,
    InvalidCapabilityError,
    RunAs,
    RunAsMismatchError,
    generate_keypair_pem,
    load_public_key_pem,
    mint_capability,
    private_key_from_seed,
    public_pem,
    verify_capability,
)

# Deterministic fixture key: fixed 32-byte seed → fixed Ed25519 key → reproducible vector.
_FIXED_SEED = bytes(range(1, 33))
_ARGV = ["bash", "-lc", "echo hi"]


def _claims(*, host="wyrm2", run_as=RunAs.ROOT, argv=None, cwd="/home/agentydragon", exp_in=3600):
    return CapabilityClaims(
        host=host, run_as=run_as, argv=argv or _ARGV, cwd=cwd, nonce="test-nonce-1", exp=int(time.time()) + exp_in
    )


def test_mint_verify_roundtrip():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims()
    token = mint_capability(claims, sk)
    got = verify_capability(token, sk.public_key(), host=claims.host, run_as=claims.run_as, argv=claims.argv)
    assert got == claims


def test_wrong_public_key_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims()
    token = mint_capability(claims, sk)
    _, other_pub = generate_keypair_pem()
    with pytest.raises(InvalidCapabilityError):
        verify_capability(
            token, load_public_key_pem(other_pub), host=claims.host, run_as=claims.run_as, argv=claims.argv
        )


def test_tampered_token_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims()
    token = mint_capability(claims, sk)
    tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
    with pytest.raises(InvalidCapabilityError):
        verify_capability(tampered, sk.public_key(), host=claims.host, run_as=claims.run_as, argv=claims.argv)


def test_expired_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims(exp_in=-10)
    token = mint_capability(claims, sk)
    with pytest.raises(InvalidCapabilityError):
        verify_capability(token, sk.public_key(), host=claims.host, run_as=claims.run_as, argv=claims.argv)


def test_wrong_audience_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    payload = _claims().to_payload() | {"aud": "some-other-aud"}
    token = pyjwt.encode(payload, sk, algorithm="EdDSA")
    with pytest.raises(InvalidCapabilityError):
        verify_capability(token, sk.public_key(), host="wyrm2", run_as=RunAs.ROOT, argv=_ARGV)


def test_missing_exp_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    payload = {
        "aud": CAPABILITY_AUDIENCE,
        "host": "wyrm2",
        "run_as": "root",
        "argv": ["true"],
        "cwd": None,
        "nonce": "n",
    }
    token = pyjwt.encode(payload, sk, algorithm="EdDSA")
    with pytest.raises(InvalidCapabilityError):
        verify_capability(token, sk.public_key(), host="wyrm2", run_as=RunAs.ROOT, argv=["true"])


def test_argv_swap_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims()
    token = mint_capability(claims, sk)
    with pytest.raises(ArgvMismatchError):
        verify_capability(token, sk.public_key(), host=claims.host, run_as=claims.run_as, argv=["rm", "-rf", "/"])


def test_run_as_swap_rejected():
    sk = private_key_from_seed(_FIXED_SEED)
    claims = _claims(run_as=RunAs.ROOT)
    token = mint_capability(claims, sk)
    with pytest.raises(RunAsMismatchError):
        verify_capability(token, sk.public_key(), host=claims.host, run_as=RunAs.AGENTYDRAGON, argv=claims.argv)


def test_emit_rust_vector(capsys):
    """Emit the fixed-key public PEM + Python-signed capability JWTs for the Rust verifier's test.

    Deterministic (fixed seed + fixed exps), so the printed tokens are stable cross-language
    vectors — a valid token plus expired/wrong-aud tokens for the Rust reject cases (so no
    private key lands in the repo). Not an assertion; run with `--test_output=all` to capture.
    """
    sk = private_key_from_seed(_FIXED_SEED)
    valid = mint_capability(
        CapabilityClaims(
            host="wyrm2", run_as=RunAs.ROOT, argv=_ARGV, cwd="/home/agentydragon", nonce="vector-nonce", exp=4102444800
        ),
        sk,
    )
    expired = mint_capability(
        CapabilityClaims(
            host="wyrm2", run_as=RunAs.ROOT, argv=_ARGV, cwd="/home/agentydragon", nonce="v", exp=1000000000
        ),
        sk,
    )
    base = CapabilityClaims(
        host="wyrm2", run_as=RunAs.ROOT, argv=_ARGV, cwd="/home/agentydragon", nonce="v", exp=4102444800
    ).to_payload()
    wrong_aud = pyjwt.encode(base | {"aud": "not-a-capability"}, sk, algorithm="EdDSA")
    with capsys.disabled():
        print("\nRUST_VECTOR_PUBLIC_PEM_BEGIN")
        print(public_pem(sk.public_key()).decode(), end="")
        print("RUST_VECTOR_PUBLIC_PEM_END")
        print("RUST_VECTOR_JWT_VALID=" + valid)
        print("RUST_VECTOR_JWT_EXPIRED=" + expired)
        print("RUST_VECTOR_JWT_WRONGAUD=" + wrong_aud)


if __name__ == "__main__":
    pytest_bazel.main()
