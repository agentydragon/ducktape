from pathlib import Path

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr

from att_gateway.pages import parse_syslog
from att_gateway.settings import Syslog, SyslogLevel
from att_gateway.syslog_reconciler import reconcile
from att_gateway.testing.fake_login import ACCESS_CODE, LOGIN_FORM, handle_login, is_logged_in

_TESTDATA = Path(__file__).parent / "testdata"
_BASE_URL = "http://gateway.test"
# The nonce in the saved `syslog.ha`.
_SYSLOG_NONCE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_DESIRED = Syslog(enabled=True, server="192.0.2.10", port=1514, level=SyslogLevel.NOTICE)


def _syslog_page(syslog: Syslog) -> str:
    """The saved `syslog.ha` (syslog off, level Error) showing `syslog` instead."""
    html = (_TESTDATA / "syslog.html").read_text()
    html = html.replace('<option value="off"\n selected="selected">', '<option value="off"\n>')
    html = html.replace('<option value="Error"\n selected="selected">', '<option value="Error"\n>')
    state = "on" if syslog.enabled else "off"
    html = html.replace(f'<option value="{state}"\n>', f'<option value="{state}"\n selected="selected">')
    html = html.replace(f'<option value="{syslog.level}"\n>', f'<option value="{syslog.level}"\n selected="selected">')
    html = html.replace('name="location" value=""', f'name="location" value="{syslog.server}"')
    return html.replace('name="port" value="514"', f'name="port" value="{syslog.port}"')


class FakeGateway:
    """Serves `syslog.ha` only to a logged-in session, and applies a post carrying the page's nonce."""

    def __init__(self, syslog: Syslog) -> None:
        self.syslog = syslog
        self.posts: list[dict[str, str]] = []
        # Like a firmware that answers a save without applying it.
        self.ignore_saves = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        page = request.url.path.removeprefix("/cgi-bin/").removesuffix(".ha")
        if page == "login":
            return handle_login(request, redirect_to="/cgi-bin/syslog.ha")
        assert page == "syslog"
        if not is_logged_in(request):
            return httpx.Response(200, text=LOGIN_FORM)
        if request.method == "POST":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.posts.append(form)
            if form["nonce"] == _SYSLOG_NONCE and not self.ignore_saves:
                self.syslog = Syslog(
                    enabled=form["syslog"] == "on",
                    server=form["location"],
                    port=int(form["port"]),
                    level=SyslogLevel(form["level"]),
                )
        return httpx.Response(200, text=_syslog_page(self.syslog))


async def _reconcile(gateway: FakeGateway, access_code: str = ACCESS_CODE) -> bool:
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle), base_url=_BASE_URL) as client:
        return await reconcile(client, _DESIRED, SecretStr(access_code), page_gap_seconds=0)


def test_fake_renders_saved_page_unchanged() -> None:
    off = Syslog(enabled=False, server="", port=514, level=SyslogLevel.ERROR)
    assert _syslog_page(off) == (_TESTDATA / "syslog.html").read_text()
    assert parse_syslog(_syslog_page(_DESIRED)).syslog == _DESIRED


async def test_differing_setting_is_saved_after_logging_in() -> None:
    gateway = FakeGateway(Syslog(enabled=False, server="", port=514, level=SyslogLevel.ERROR))
    assert await _reconcile(gateway)
    assert gateway.syslog == _DESIRED
    assert gateway.posts == [
        {
            "nonce": _SYSLOG_NONCE,
            "syslog": "on",
            "location": "192.0.2.10",
            "port": "1514",
            "level": "Notice",
            "Save": "Save",
        }
    ]


async def test_configured_setting_is_left_alone() -> None:
    gateway = FakeGateway(_DESIRED)
    assert not await _reconcile(gateway)
    assert gateway.posts == []


async def test_save_not_taking_raises() -> None:
    gateway = FakeGateway(Syslog(enabled=False, server="", port=514, level=SyslogLevel.ERROR))
    gateway.ignore_saves = True
    with pytest.raises(RuntimeError, match="after saving"):
        await _reconcile(gateway)


async def test_wrong_access_code_raises_without_saving() -> None:
    gateway = FakeGateway(Syslog(enabled=False, server="", port=514, level=SyslogLevel.ERROR))
    with pytest.raises(ValueError, match="syslog form not found"):
        await _reconcile(gateway, access_code="wrong-code")
    assert gateway.posts == []


if __name__ == "__main__":
    pytest_bazel.main()
