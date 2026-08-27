"""Validation for deploy-configured plain HTTP(S) endpoint URLs."""

from urllib.parse import urlsplit


def uncredentialed_http_url(value: str, *, field_name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an HTTP(S) URL without credentials or a fragment")
    return value
