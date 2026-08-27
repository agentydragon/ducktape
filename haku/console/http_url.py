"""Validation for deploy-configured plain HTTP(S) endpoint URLs."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator


def _uncredentialed_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("must be an HTTP(S) URL without credentials or a fragment")
    return value


type UncredentialedHttpUrl = Annotated[str, AfterValidator(_uncredentialed_http_url)]
