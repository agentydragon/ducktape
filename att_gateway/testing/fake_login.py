"""The gateway's form login (`att_gateway.login`), faked for the tests of its clients."""

import hashlib

import httpx

ACCESS_CODE = "test-code-1"
_NONCE = "0123abcd"
LOGIN_FORM = f'<html><body><form><input type="hidden" name="nonce" value="{_NONCE}" /></form></body></html>'


def handle_login(request: httpx.Request, redirect_to: str) -> httpx.Response:
    """Answers `login.ha`: grants the session that posts `ACCESS_CODE` hashed with the nonce,
    redirecting it to `redirect_to` as the live gateway redirects to the page that sent it there."""
    if request.method == "POST":
        form = dict(httpx.QueryParams(request.content.decode()))
        if form["hashpassword"] == hashlib.md5(f"{ACCESS_CODE}{_NONCE}".encode()).hexdigest():
            return httpx.Response(302, headers={"Location": redirect_to, "Set-Cookie": "SessionID=granted"})
        return httpx.Response(200, text=LOGIN_FORM)
    return httpx.Response(200, text=LOGIN_FORM, headers={"Set-Cookie": "SessionID=anonymous"})


def is_logged_in(request: httpx.Request) -> bool:
    return request.headers.get_list("Cookie") == ["SessionID=granted"]
