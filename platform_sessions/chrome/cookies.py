"""Chrome cookie decryption shared by local session exporters."""

import hashlib
import subprocess

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


class ChromeCookieError(ValueError):
    """A Chrome cookie could not be read."""


def chrome_password() -> bytes:
    # Chrome on Linux stores its v11 encryption password in the desktop Secret Service.
    result = subprocess.run(
        ["secret-tool", "lookup", "application", "chrome"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    password = result.stdout.removesuffix(b"\n")
    if not password:
        raise ChromeCookieError("Chrome's desktop keyring password was empty.")
    return password


def decrypt_cookie(encrypted: bytes, host: str, version: int, password: bytes) -> str:
    prefix = encrypted[:3]
    if prefix not in {b"v10", b"v11"}:
        raise ChromeCookieError("Unsupported Chrome cookie encryption format.")
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts" if prefix == b"v10" else password, b"saltysalt", 1, 16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    try:
        padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        if version >= 24:
            digest = hashlib.sha256(host.encode()).digest()
            if plaintext[:32] != digest:
                raise ValueError("cookie host mismatch")
            plaintext = plaintext[32:]
        return plaintext.decode("utf-8")
    except ValueError, UnicodeDecodeError:
        raise ChromeCookieError("Could not decrypt a Chrome cookie with the desktop keyring.") from None
