"""Strict environment flags shared by VM system-test modules."""

import os


def strict_env_flag(name: str, *, default: str = "0") -> bool:
    value = os.environ.get(name, default)
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be '0' or '1', got {value!r}")
    return value == "1"
