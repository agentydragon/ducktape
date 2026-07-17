"""Console-signed exec capability — the per-approval authorization `hostexecd` verifies.

The console (the trust boundary) signs a capability at approval time; the host-side
`hostexecd` verifies it before executing. The capability binds the *exact* approved command so
a compromised relay cannot swap or replay it. It is one of the two independent checks
`hostexecd` requires — the other is the operator's Authentik token (which carries the revocable
`hostexec-<run_as>-<host>` authorization); this countersignature carries the command binding.

**Cross-language contract.** The console signs here (Python); `hostexecd` verifies in Rust. The
signed bytes are a fixed, injection-proof, length-prefixed framing (`_signing_message`) so both
languages produce identical bytes. `testdata/capability_vectors.json` pins the wire format and
signature for the Rust side to reproduce. Change either encoding function only in lockstep with
the Rust verifier and the vectors.

Signature scheme: Ed25519 (RFC 8032, deterministic) over the framed message. Keys are raw
32-byte values, hex-encoded on the wire (ed25519-dalek consumes raw bytes directly).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, Field

# Domain-separation tags: distinct constants so a signature/argv digest can never be replayed
# in a different context. Bump the version suffix on any framing change (lockstep with Rust).
_CAPABILITY_DOMAIN = b"ducktape.hostexec.capability.v1"
_ARGV_DOMAIN = b"ducktape.hostexec.argv.v1"


class RunAs(StrEnum):
    """The POSIX user a command runs as on the target host."""

    AGENTYDRAGON = "agentydragon"
    ROOT = "root"


def _frame(b: bytes) -> bytes:
    """Length-prefixed segment: 4-byte big-endian length, then the bytes.

    Unambiguous concatenation — no delimiter a field value could inject.
    """
    return len(b).to_bytes(4, "big") + b


def _frame_optional(s: str | None) -> bytes:
    """A `str | None` field: a presence byte, then the framed UTF-8 (absent = just `\\x00`).

    Distinguishes `None` (no cwd) from `""` — they must not collide in the signed message.
    """
    if s is None:
        return b"\x00"
    return b"\x01" + _frame(s.encode("utf-8"))


def argv_digest(argv: list[str]) -> bytes:
    """SHA-256 over a canonical, length-prefixed encoding of the argv vector.

    Binds the exact argument list (count + each arg's exact bytes) with no delimiter ambiguity.
    """
    h = hashlib.sha256()
    h.update(_ARGV_DOMAIN)
    h.update(len(argv).to_bytes(4, "big"))
    for arg in argv:
        h.update(_frame(arg.encode("utf-8")))
    return h.digest()


@dataclass(frozen=True)
class CapabilityClaims:
    """The signed content of a capability: the exact command the operator approved.

    `argv_sha256` is the `argv_digest` of the approved argv (not the argv itself — keeps the
    capability compact and fixed-size). `hostexecd` recomputes it from the request's argv and
    rejects a mismatch, then verifies the signature over this whole framed claim set.
    """

    host: str
    run_as: RunAs
    argv_sha256: bytes  # 32-byte argv_digest
    cwd: str | None
    nonce: bytes  # single-use; hostexecd rejects a replayed nonce
    exp: int  # unix seconds; hostexecd rejects once now > exp

    def signing_message(self) -> bytes:
        """The exact bytes signed/verified. Must match the Rust verifier byte-for-byte."""
        if len(self.argv_sha256) != 32:
            raise ValueError(f"argv_sha256 must be 32 bytes, got {len(self.argv_sha256)}")
        return b"".join(
            [
                _CAPABILITY_DOMAIN,
                _frame(self.host.encode("utf-8")),
                _frame(self.run_as.encode("utf-8")),
                _frame(self.argv_sha256),
                _frame_optional(self.cwd),
                _frame(self.nonce),
                self.exp.to_bytes(8, "big", signed=False),
            ]
        )


class InvalidCapabilityError(Exception):
    """A capability failed verification. Subclasses name the specific failure for audit logs."""


class BadSignatureError(InvalidCapabilityError):
    """The Ed25519 signature did not verify under the trusted console public key."""


class ArgvMismatchError(InvalidCapabilityError):
    """The request's argv does not hash to the capability's bound `argv_sha256`."""


class RunAsMismatchError(InvalidCapabilityError):
    """The request's `run_as`/`host` disagree with the signed capability."""


class ExpiredError(InvalidCapabilityError):
    """`now` is past the capability's `exp`."""


def sign_capability(claims: CapabilityClaims, private_key: Ed25519PrivateKey) -> bytes:
    """Sign the claims with the console's Ed25519 private key. Returns the 64-byte signature."""
    return private_key.sign(claims.signing_message())


def verify_signature(claims: CapabilityClaims, signature: bytes, public_key: Ed25519PublicKey) -> None:
    """Verify the signature over `claims`. Raises `BadSignatureError` if it does not verify."""
    try:
        public_key.verify(signature, claims.signing_message())
    except InvalidSignature as exc:
        raise BadSignatureError("capability signature did not verify") from exc


def check_capability(
    claims: CapabilityClaims,
    signature: bytes,
    *,
    public_key: Ed25519PublicKey,
    host: str,
    run_as: RunAs,
    argv: list[str],
    now: int,
) -> None:
    """Full fail-closed check `hostexecd` performs (Python reference; the Rust verifier must match).

    Verifies, in order: host/run_as agreement, argv binding, expiry, signature. Raises the
    specific `InvalidCapabilityError` subclass on the first failure. Nonce single-use is enforced by
    the caller's replay store, not here (it is host-local state, not a property of the claims).
    """
    if claims.host != host or claims.run_as != run_as:
        raise RunAsMismatchError(f"capability {claims.host=}/{claims.run_as=} != request {host=}/{run_as=}")
    if claims.argv_sha256 != argv_digest(argv):
        raise ArgvMismatchError("request argv does not match the approved command")
    if now > claims.exp:
        raise ExpiredError(f"capability expired at {claims.exp}, now {now}")
    verify_signature(claims, signature, public_key)


class SignedCapability(BaseModel):
    """Wire form of a signed capability (hex-encoded bytes), carried in the `hostexecd` request.

    Console → `hostexec-mcp` → `hostexecd`. `hostexecd` parses this, reconstructs the claims, and
    runs the equivalent of `check_capability`.
    """

    host: str
    run_as: RunAs
    argv_sha256: str = Field(description="hex-encoded 32-byte argv digest")
    cwd: str | None = None
    nonce: str = Field(description="hex-encoded single-use nonce")
    exp: int = Field(description="unix seconds expiry")
    signature: str = Field(description="hex-encoded 64-byte Ed25519 signature")

    @classmethod
    def create(cls, claims: CapabilityClaims, private_key: Ed25519PrivateKey) -> SignedCapability:
        """Sign `claims` and package them for the wire."""
        return cls(
            host=claims.host,
            run_as=claims.run_as,
            argv_sha256=claims.argv_sha256.hex(),
            cwd=claims.cwd,
            nonce=claims.nonce.hex(),
            exp=claims.exp,
            signature=sign_capability(claims, private_key).hex(),
        )

    def claims(self) -> CapabilityClaims:
        """Reconstruct the signed claims from the wire fields."""
        return CapabilityClaims(
            host=self.host,
            run_as=self.run_as,
            argv_sha256=bytes.fromhex(self.argv_sha256),
            cwd=self.cwd,
            nonce=bytes.fromhex(self.nonce),
            exp=self.exp,
        )

    def signature_bytes(self) -> bytes:
        return bytes.fromhex(self.signature)


def generate_keypair() -> tuple[str, str]:
    """Generate a console signing keypair. Returns `(private_seed_hex, public_key_hex)` (raw 32B each)."""
    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes_raw().hex(), sk.public_key().public_bytes_raw().hex()


def load_private_key(seed_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))


def load_public_key(public_hex: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
