"""Console-signed exec capability — an EdDSA JWT the console mints and `hostexecd` verifies.

The console (the trust boundary) mints a capability at approval time; the host-side `hostexecd`
verifies it before executing. The capability binds the *exact* approved command so a compromised
relay cannot swap or replay it. It is one of the two independent checks `hostexecd` requires —
the other is the operator's Authentik token (which carries the revocable
`hostexec-<run_as>-<host>` authorization); this countersignature carries the command binding.

**Cross-language contract.** The console mints here (Python / PyJWT); `hostexecd` verifies in
Rust (`jsonwebtoken`). Both use standard RFC 7519 JWT + RFC 8037 EdDSA (Ed25519), so interop is
guaranteed by the standard — no hand-rolled encoding to keep in lockstep. The JWT carries the
approved command directly in its claims; `hostexecd` checks the signature + `aud` + `exp`, then
cross-checks that the request's `host`/`run_as`/`argv` equal the signed claims.
"""

from __future__ import annotations

from enum import StrEnum

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel

# Audience pins these JWTs to the capability purpose so an Authentik token (or any other EdDSA
# JWT) can never be replayed as a capability, and vice versa.
CAPABILITY_AUDIENCE = "hostexec-capability"
_ALGORITHM = "EdDSA"


class RunAs(StrEnum):
    """The POSIX user a command runs as on the target host."""

    AGENTYDRAGON = "agentydragon"
    ROOT = "root"


class CapabilityClaims(BaseModel):
    """The approved command, carried as JWT claims and cross-checked against the request.

    `hostexecd` requires the request's `host`/`run_as`/`argv` to equal these signed values, so a
    relay cannot substitute a different command than the operator approved.
    """

    host: str
    run_as: RunAs
    argv: list[str]
    cwd: str | None = None
    nonce: str  # single-use; hostexecd rejects a replayed nonce (host-local replay store)
    exp: int  # unix seconds; JWT `exp` — verification rejects once expired

    def to_payload(self) -> dict[str, object]:
        return {
            "aud": CAPABILITY_AUDIENCE,
            "host": self.host,
            "run_as": self.run_as.value,
            "argv": self.argv,
            "cwd": self.cwd,
            "nonce": self.nonce,
            "exp": self.exp,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> CapabilityClaims:
        return cls(
            host=payload["host"],
            run_as=RunAs(payload["run_as"]),
            argv=payload["argv"],
            cwd=payload.get("cwd"),
            nonce=payload["nonce"],
            exp=payload["exp"],
        )


class InvalidCapabilityError(Exception):
    """A capability failed verification. Subclasses name the specific failure for audit logs."""


class RunAsMismatchError(InvalidCapabilityError):
    """The request's `host`/`run_as` disagree with the signed capability."""


class ArgvMismatchError(InvalidCapabilityError):
    """The request's argv is not the argv the capability approved."""


def mint_capability(claims: CapabilityClaims, private_key: Ed25519PrivateKey) -> str:
    """Mint (sign) a capability JWT with the console's Ed25519 private key."""
    return jwt.encode(claims.to_payload(), private_key, algorithm=_ALGORITHM)


def verify_capability(
    token: str, public_key: Ed25519PublicKey, *, host: str, run_as: RunAs, argv: list[str]
) -> CapabilityClaims:
    """Fail-closed verification `hostexecd` performs (Python reference; the Rust verifier matches).

    Verifies the JWT signature, audience, and expiry (via PyJWT — `exp` required), then
    cross-checks that the request's `host`/`run_as`/`argv` equal the signed claims. Raises the
    specific `InvalidCapabilityError` subclass on failure. Nonce single-use is the caller's
    replay-store concern (host-local state), not a property of the token.
    """
    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[_ALGORITHM],
            audience=CAPABILITY_AUDIENCE,
            options={"require": ["exp", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        # Bad signature, expired, or wrong audience — one domain failure for the caller.
        raise InvalidCapabilityError(str(exc)) from exc
    claims = CapabilityClaims.from_payload(payload)
    if claims.host != host or claims.run_as != run_as:
        raise RunAsMismatchError(f"capability {claims.host=}/{claims.run_as=} != request {host=}/{run_as=}")
    if claims.argv != argv:
        raise ArgvMismatchError("request argv does not match the approved command")
    return claims


def generate_keypair_pem() -> tuple[bytes, bytes]:
    """Generate a console signing keypair as PEM. Returns `(private_pkcs8_pem, public_spki_pem)`.

    PEM is what both PyJWT (private) and Rust `jsonwebtoken` (public, `from_ed_pem`) consume.
    """
    sk = Ed25519PrivateKey.generate()
    return _private_pem(sk), public_pem(sk.public_key())


def private_key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    """Deterministic key from a 32-byte seed (test fixtures / reproducible vectors)."""
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )


def load_public_key_pem(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(f"expected an Ed25519 public key, got {type(key).__name__}")
    return key


def _private_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
