"""The BGW320 web UI's form login, which opens the pages behind the device access code."""

from __future__ import annotations

import asyncio
import hashlib
import re

import httpx
from pydantic import SecretStr

_NONCE = re.compile(r'name="nonce" value="([0-9a-f]+)"')


async def log_in(client: httpx.AsyncClient, access_code: SecretStr, page_gap_seconds: float) -> None:
    """The web UI's form login. The first visit sets the session cookie, the second serves
    the form with a nonce bound to that session, and the form posts the code hashed with
    the nonce; the session cookie then opens the pages behind the code."""
    await client.get("/cgi-bin/login.ha")
    await asyncio.sleep(page_gap_seconds)
    form = await client.get("/cgi-bin/login.ha")
    form.raise_for_status()
    nonce = _NONCE.search(form.text)
    if nonce is None:
        raise ValueError("no nonce in the login form")
    code = access_code.get_secret_value()
    await asyncio.sleep(page_gap_seconds)
    response = await client.post(
        "/cgi-bin/login.ha",
        data={
            "nonce": nonce.group(1),
            "password": "*" * len(code),
            "hashpassword": hashlib.md5((code + nonce.group(1)).encode(), usedforsecurity=False).hexdigest(),
            "Continue": "Continue",
        },
    )
    response.raise_for_status()
