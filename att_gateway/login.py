"""The BGW320 web UI's form login, which opens the pages behind the device access code."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable

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
    # A good login answers with a redirect to the page that sent the session to the form; a
    # wrong code with the form again, which the caller sees when the page it wanted still
    # serves the form.
    if response.is_error:
        response.raise_for_status()


async def read_page[T](
    client: httpx.AsyncClient,
    page: str,
    parse: Callable[[str], T],
    access_code: SecretStr | None,
    page_gap_seconds: float,
) -> T:
    """Protected pages may serve the login form with HTTP 200. Retry once after login."""

    async def fetch() -> T:
        response = await client.get(f"/cgi-bin/{page}.ha")
        response.raise_for_status()
        return parse(response.text)

    try:
        return await fetch()
    except ValueError:
        if access_code is None:
            raise
    await asyncio.sleep(page_gap_seconds)
    await log_in(client, access_code, page_gap_seconds)
    await asyncio.sleep(page_gap_seconds)
    return await fetch()
