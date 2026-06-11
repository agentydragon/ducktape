"""Token generation and verification scheme."""

import hmac
import math
from datetime import datetime
from hashlib import blake2b, sha256

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

N_TAGS = 7
TAG_LEN = 2


def _b58enc(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(reversed(out)).rjust(length, ALPHABET[0])


def bytes_b58(x: bytes, len: int) -> str:
    return _b58enc(int.from_bytes(x, "big"), len)


def _digest_size(chars: int) -> int:
    """Bytes needed so that `chars` base-58 digits can represent it."""
    return math.ceil(chars * math.log2(58) / 8)


class VerificationError(ValueError):
    """Aggregates all individual verification failures."""

    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


class TokenScheme:
    """
    Token v1  (21 chars)
    --------------------
    1:MMDD-HH:MM-PPPAaaaaaa

    - Version prefix ("1:")
    - Human-readable month-day-hour-minute
    - 3-char base58 public hash
    - blake2b(date‖pepper) (~12 bits quick check)

    Rejects obvious typos without the secret; authenticates quickly with it.
    """

    _VERSION = "1"

    # ─── master knobs: base-58 symbol counts ─────────────────────────────
    _DOC_LEN = 3
    _PUB_LEN = 3
    _AUTH_LEN = 8

    def __init__(self, secret: bytes, doc: str):
        self.secret = secret
        self.doc = doc

    def _doc_hash(self) -> str:
        size = _digest_size(self._DOC_LEN)
        return bytes_b58(sha256(self.doc.encode()).digest()[:size], self._DOC_LEN)

    def make_token(self, now: datetime) -> tuple[str, list[str]]:
        date = now.strftime("%m%d-%H:%M")

        doc_hash = self._doc_hash()
        pub_txt = self._public_auth(date)
        auth_txt = self._private_auth(date)

        prefix = f"{self._VERSION}:{date}-"
        suffix = f"{doc_hash}{pub_txt}{auth_txt}"

        assert self._DOC_LEN + self._PUB_LEN + self._AUTH_LEN == N_TAGS * TAG_LEN

        return prefix, [suffix[i : i + 2] for i in range(0, len(suffix), 2)]

    def _public_auth(self, date: str) -> str:
        size = _digest_size(self._PUB_LEN)
        return bytes_b58(blake2b(date.encode(), digest_size=size).digest(), self._PUB_LEN)

    def _private_auth(self, date: str) -> str:
        size = _digest_size(self._AUTH_LEN)
        digest = hmac.new(self.secret, date.encode(), sha256).digest()[:size]
        return bytes_b58(digest, self._AUTH_LEN)

    def verify_token(self, token: str):
        """Validate *token* against the current document & secret."""
        issues: list[str] = []

        if ":" not in token:
            raise VerificationError(["Token is missing ':' separator"])

        version_str, payload = token.split(":", 1)

        if version_str != self._VERSION:
            issues.append(f"Version mismatch (expected={self._VERSION}, got={version_str or '<empty>'})")

        parts = payload.split("-", 2)
        if len(parts) < 2:
            issues.append("Token is incomplete - expected date & digest parts")
            raise VerificationError(issues)

        mmdd, hhmm = parts[0], parts[1]
        digest = parts[2] if len(parts) == 3 else ""

        date_valid = True
        if len(mmdd) != 4 or not mmdd.isdigit():
            issues.append(f"Invalid MMDD component: '{mmdd}'")
            date_valid = False

        if len(hhmm) != 5 or hhmm[2] != ":" or not (hhmm[:2].isdigit() and hhmm[3:].isdigit()):
            issues.append(f"Invalid HH:MM component: '{hhmm}'")
            date_valid = False

        date = f"{mmdd}-{hhmm}" if date_valid else None

        doc_act = digest[: self._DOC_LEN]
        pub_act = digest[self._DOC_LEN : self._DOC_LEN + self._PUB_LEN]
        priv_act = digest[self._DOC_LEN + self._PUB_LEN :]

        if len(doc_act) != self._DOC_LEN:
            issues.append(f"Document hash incomplete ({len(doc_act)}/{self._DOC_LEN} characters provided)")
        else:
            doc_exp = self._doc_hash()
            if doc_act != doc_exp:
                issues.append(f"Document hash mismatch (expected={doc_exp}, got={doc_act})")

        if len(pub_act) != self._PUB_LEN:
            issues.append(f"Public hash incomplete ({len(pub_act)}/{self._PUB_LEN} characters provided)")
        elif date is None:
            issues.append("Cannot verify public hash due to invalid date")
        else:
            pub_exp = self._public_auth(date)
            if pub_act != pub_exp:
                issues.append(f"Public hash mismatch (expected={pub_exp}, got={pub_act})")

        if len(priv_act) != self._AUTH_LEN:
            issues.append(f"Private hash incomplete ({len(priv_act)}/{self._AUTH_LEN} characters provided)")
        elif date is None:
            issues.append("Cannot verify private hash due to invalid date")
        else:
            priv_exp = self._private_auth(date)
            if not hmac.compare_digest(priv_act, priv_exp):
                issues.append(f"Private hash mismatch (expected={priv_exp}, got={priv_act})")

        if issues:
            raise VerificationError(issues)
