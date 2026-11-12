"""Convert Chrome profile cookies to the shared scoped representation."""

from typing import Literal

from platform_sessions.chrome.profile import ChromeCookie
from platform_sessions.scoped_cookie import ScopedCookie


def from_chrome[Cookie: ScopedCookie](cookie: ChromeCookie, cookie_type: type[Cookie]) -> Cookie:
    if cookie.partition:
        raise ValueError("Partitioned cookies cannot be represented")
    same_sites: dict[int, Literal["Strict", "Lax", "None"]] = {0: "None", 1: "Lax", 2: "Strict"}
    return cookie_type(
        name=cookie.name,
        value=cookie.plaintext(),
        domain=cookie.host,
        path=cookie.path,
        secure=bool(cookie.secure),
        httpOnly=bool(cookie.http_only),
        expires=float(cookie.expires_utc // 1_000_000 - 11644473600) if cookie.expires_utc else -1.0,
        sameSite=same_sites.get(cookie.same_site, "Lax"),
    )
