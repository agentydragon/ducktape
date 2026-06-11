"""Password hashing helpers using Argon2."""

from passlib.hash import argon2


def hash_password(password: str) -> str:
    """Return Argon2 hash of ``password``."""

    return argon2.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify ``password`` against ``hashed`` Argon2 digest."""

    try:
        return argon2.verify(password, hashed)
    except Exception:  # pragma: no cover - passlib raises various errors
        return False
