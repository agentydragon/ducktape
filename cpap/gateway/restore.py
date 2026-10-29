"""Restore the ez Share card's saved WiFi credentials over its local HTTP API."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import Message
from html.parser import HTMLParser
from typing import IO

DEFAULT_SSID = "ez Share"
DEFAULT_WIFI_PASSWORD = "88888888"
_FACTORY_ADMIN_PASSWORD = "admin"
REQUEST_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 1024 * 1024


class RestoreError(Exception):
    """A safe-to-display failure from the card restore flow."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: Message, newurl: str
    ) -> None:
        return None


class _ChannelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_channel_select = False
        self._in_option = False
        self._option_selected = False
        self._option_value: str | None = None
        self._option_text: list[str] = []
        self.selected_values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "select":
            self._in_channel_select = dict(attrs).get("id") == "channel"
        elif tag == "option" and self._in_channel_select:
            attributes = dict(attrs)
            self._in_option = True
            self._option_selected = "selected" in attributes
            self._option_value = attributes.get("value")
            self._option_text = []

    def handle_data(self, data: str) -> None:
        if self._in_option:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_option:
            if self._option_selected:
                value = self._option_value
                if value is None:
                    value = "".join(self._option_text).strip()
                self.selected_values.append(value)
            self._in_option = False
        elif tag == "select" and self._in_channel_select:
            self._in_channel_select = False


def _valid_mac(value: str) -> bool:
    return re.fullmatch(r"(?:[0-9A-Fa-f]{12}|(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})", value.strip()) is not None


def _normalize_mac(value: str) -> str:
    return value.strip().replace(":", "").replace("-", "").upper()


def _validated_base_url(base_url: str) -> str:
    parsed: urllib.parse.SplitResult | None
    try:
        parsed = urllib.parse.urlsplit(base_url)
        _ = parsed.port
    except TypeError, ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RestoreError("Invalid card restore settings.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _get(opener: urllib.request.OpenerDirector, url: str) -> bytes:
    response_body: bytes | None = None
    request_failed = False
    try:
        with opener.open(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status == 200:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        # Leave the transport exception behind: it can include the full query URL.
        request_failed = True
    if request_failed or response_body is None:
        raise RestoreError("Card request failed.")
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise RestoreError("Card response was too large.")
    return response_body


def _parse_xml(data: bytes) -> ET.Element:
    root: ET.Element | None
    try:
        # Decode the card's encoding before removing its declaration for Expat.
        xml_text = data.decode("gb2312")
        xml_text = re.sub(r"^\s*<\?xml[^?]*\?>", "", xml_text, count=1)
        root = ET.fromstring(xml_text)
    except ET.ParseError, LookupError, ValueError:
        root = None
    if root is None:
        raise RestoreError("Card returned an invalid response.")
    return root


def _get_channel(data: bytes) -> str:
    parser = _ChannelParser()
    html: str | None
    try:
        html = data.decode("gb2312")
        parser.feed(html)
        parser.close()
    except LookupError, UnicodeDecodeError, ValueError:
        html = None
    if html is None or len(parser.selected_values) != 1:
        raise RestoreError("Could not read the card's current channel.")
    channel = parser.selected_values[0]
    if re.fullmatch(r"[0-9]{1,2}", channel) is None or not 1 <= int(channel) <= 13:
        raise RestoreError("Could not read the card's current channel.")
    return channel


def _login(opener: urllib.request.OpenerDirector, base: str, password: str, expected_mac: str) -> bool:
    query = urllib.parse.urlencode({"client": "1", "password": password}, quote_via=urllib.parse.quote)
    login_xml = _parse_xml(_get(opener, f"{base}/publicdir/sendurl?{query}"))
    login_result = login_xml.findtext("./ezaction/clientLogin")
    if login_result == "0":
        return False
    login_mac = (login_xml.findtext("./ezaction/mac") or "").strip()
    if login_result != "1" or not _valid_mac(login_mac) or _normalize_mac(login_mac) != expected_mac:
        raise RestoreError("Card login was rejected.")
    return True


def restore_card(*, base_url: str, expected_mac: str, ssid: str, wifi_password: str, admin_password: str) -> None:
    """Restore credentials only after verifying the default card identity and login."""
    if (
        not _valid_mac(expected_mac)
        or re.fullmatch(r"[A-Za-z0-9_ ]{1,32}", ssid) is None
        or re.fullmatch(r"[A-Za-z0-9_]{8,63}", wifi_password) is None
        or re.fullmatch(r"[A-Za-z0-9_ ]{1,8}", admin_password) is None
    ):
        raise RestoreError("Invalid card restore settings.")

    normalized_expected_mac = _normalize_mac(expected_mac)
    base = _validated_base_url(base_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())

    identity_xml = _parse_xml(_get(opener, f"{base}/client?command=devicemac"))
    card_mac = (identity_xml.findtext("./device/mac") or "").strip()
    card_ssid = (identity_xml.findtext("./device/ssid") or "").strip()
    if not _valid_mac(card_mac) or _normalize_mac(card_mac) != normalized_expected_mac or card_ssid != DEFAULT_SSID:
        raise RestoreError("Card identity did not match the expected card.")

    if not _login(opener, base, _FACTORY_ADMIN_PASSWORD, normalized_expected_mac) and (
        admin_password == _FACTORY_ADMIN_PASSWORD or not _login(opener, base, admin_password, normalized_expected_mac)
    ):
        raise RestoreError("Card login was rejected.")

    channel = _get_channel(_get(opener, f"{base}/config?vtype=0"))
    config_query = urllib.parse.urlencode(
        {"auth": admin_password, "ssid": ssid, "pass": wifi_password, "channel": channel}, quote_via=urllib.parse.quote
    )
    config_result = _get(opener, f"{base}/config?{config_query}")
    if b"config_success" not in config_result:
        raise RestoreError("Card rejected the configuration update.")

    _get(opener, f"{base}/config?reset=1")
