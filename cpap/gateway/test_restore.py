from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_bazel

from cpap.gateway.restore import RestoreError, restore_card

EXPECTED_MAC = "02:13:24:35:46:57"
NEW_SSID = "Test AP_1"
NEW_WIFI_PASSWORD = "Wifi_1234"
NEW_ADMIN_PASSWORD = "Ad min_1"


@dataclass
class _CardState:
    mac: str = EXPECTED_MAC
    ssid: str = "ez Share"
    login_success: bool = True
    accepted_admin_password: str = "admin"
    login_mac: str | None = None
    config_result: str = "success"
    redirect_target: str | None = None
    requests: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    request_targets: list[str] = field(default_factory=list)
    saved_settings: dict[str, list[str]] | None = None
    applied_settings: dict[str, list[str]] | None = None
    reset_count: int = 0


class _CardServer(ThreadingHTTPServer):
    state: _CardState


class _RedirectServer(ThreadingHTTPServer):
    requests: list[str]


@dataclass(frozen=True)
class _LocalServers:
    card_url: str
    card_state: _CardState
    redirect_url: str
    redirect_server: _RedirectServer


class _CardHandler(BaseHTTPRequestHandler):
    server: _CardServer

    def do_GET(self) -> None:
        server = cast(_CardServer, self.server)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        server.state.requests.append((parsed.path, query))
        server.state.request_targets.append(self.path)
        if parsed.path == "/client" and query == {"command": ["devicemac"]}:
            self._respond(
                200,
                (
                    '<?xml version="1.0" encoding="gb2312"?>'
                    f"<response><device><mac>{server.state.mac}</mac><ssid>{server.state.ssid}</ssid>"
                    "<note>卡片</note></device></response>"
                ).encode("gb2312"),
            )
        elif parsed.path == "/publicdir/sendurl":
            login_mac = server.state.login_mac or server.state.mac
            attempted_password = query.get("password", [""])[0]
            # LZ1801EDPG:1.0.0 returns -1 for an incorrect administrator password.
            success = (
                "1"
                if server.state.login_success and attempted_password == server.state.accepted_admin_password
                else "-1"
            )
            self._respond(
                200,
                (
                    '<?xml version="1.0" encoding="gb2312"?>'
                    f"<response><ezaction><clientLogin>{success}</clientLogin><mac>{login_mac}</mac>"
                    "</ezaction></response>"
                ).encode("gb2312"),
            )
        elif parsed.path == "/config" and query == {"vtype": ["0"]}:
            self._respond(
                200,
                b'<input name="password" value="fixture-secret-field">'
                b'<select id="channel"><option value="6">6</option>'
                b'<option value="11" selected="selected">11</option></select>',
            )
        elif parsed.path == "/config" and query == {"reset": ["1"]}:
            server.state.reset_count += 1
            server.state.applied_settings = server.state.saved_settings
            self._respond(200, b"reset")
        elif parsed.path == "/config":
            server.state.saved_settings = query
            server.state.accepted_admin_password = query["auth"][0]
            if server.state.config_result == "redirect":
                assert server.state.redirect_target is not None
                self.send_response(302)
                self.send_header("Location", server.state.redirect_target)
                self.end_headers()
            elif server.state.config_result == "http_error":
                self._respond(500, b"fixture failure")
            elif server.state.config_result == "rejected":
                self._respond(200, b"config_failed")
            else:
                self._respond(200, b"config_success")
        else:
            self._respond(404, b"not found")

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=gb2312")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _RedirectHandler(BaseHTTPRequestHandler):
    server: _RedirectServer

    def do_GET(self) -> None:
        server = cast(_RedirectServer, self.server)
        server.requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def local_servers() -> Iterator[_LocalServers]:
    with ExitStack() as stack:
        card_state = _CardState()
        card_server = _CardServer(("127.0.0.1", 0), _CardHandler)
        card_server.state = card_state
        stack.enter_context(card_server)
        card_thread = Thread(target=card_server.serve_forever)
        card_thread.start()
        stack.callback(card_thread.join)
        stack.callback(card_server.shutdown)

        redirect_server = _RedirectServer(("127.0.0.1", 0), _RedirectHandler)
        redirect_server.requests = []
        stack.enter_context(redirect_server)
        redirect_thread = Thread(target=redirect_server.serve_forever)
        redirect_thread.start()
        stack.callback(redirect_thread.join)
        stack.callback(redirect_server.shutdown)

        card_host, card_port = cast(tuple[str, int], card_server.server_address)
        redirect_host, redirect_port = cast(tuple[str, int], redirect_server.server_address)
        yield _LocalServers(
            card_url=f"http://{card_host}:{card_port}",
            card_state=card_state,
            redirect_url=f"http://{redirect_host}:{redirect_port}",
            redirect_server=redirect_server,
        )


def _restore(base_url: str) -> None:
    restore_card(
        base_url=base_url,
        expected_mac=EXPECTED_MAC.lower().replace(":", "-"),
        ssid=NEW_SSID,
        wifi_password=NEW_WIFI_PASSWORD,
        admin_password=NEW_ADMIN_PASSWORD,
    )


def test_restore_preserves_channel_and_url_encodes_credentials(local_servers: _LocalServers) -> None:
    state = local_servers.card_state

    _restore(local_servers.card_url)

    assert state.saved_settings == {
        "auth": [NEW_ADMIN_PASSWORD],
        "ssid": [NEW_SSID],
        "pass": [NEW_WIFI_PASSWORD],
        "channel": ["11"],
    }
    assert "ssid=Test%20AP_1" in state.request_targets[3]
    assert state.reset_count == 1
    assert [path for path, _ in state.requests] == ["/client", "/publicdir/sendurl", "/config", "/config", "/config"]
    assert state.requests[1][1] == {"client": ["1"], "password": ["admin"]}
    assert state.requests[2][1] == {"vtype": ["0"]}
    assert state.requests[4][1] == {"reset": ["1"]}


@pytest.mark.parametrize(
    ("card_mac", "card_ssid"), [("02:13:24:35:46:58", "ez Share"), (EXPECTED_MAC, "Neighbor Card")]
)
def test_wrong_card_identity_stops_before_login_or_mutation(
    local_servers: _LocalServers, card_mac: str, card_ssid: str
) -> None:
    state = local_servers.card_state
    state.mac = card_mac
    state.ssid = card_ssid

    with pytest.raises(RestoreError):
        _restore(local_servers.card_url)

    assert [path for path, _ in state.requests] == ["/client"]
    assert state.saved_settings is None
    assert state.reset_count == 0


def test_denied_login_stops_before_configuration(local_servers: _LocalServers) -> None:
    state = local_servers.card_state
    state.login_success = False

    with pytest.raises(RestoreError):
        _restore(local_servers.card_url)

    assert [path for path, _ in state.requests] == ["/client", "/publicdir/sendurl", "/publicdir/sendurl"]
    assert state.saved_settings is None
    assert state.reset_count == 0


def test_login_must_report_the_same_card(local_servers: _LocalServers) -> None:
    state = local_servers.card_state
    state.login_mac = "02:13:24:35:46:58"

    with pytest.raises(RestoreError):
        _restore(local_servers.card_url)

    assert [path for path, _ in state.requests] == ["/client", "/publicdir/sendurl"]
    assert state.saved_settings is None
    assert state.reset_count == 0


def test_stored_admin_recovers_a_card_after_an_interrupted_restore(local_servers: _LocalServers) -> None:
    state = local_servers.card_state
    state.accepted_admin_password = NEW_ADMIN_PASSWORD

    _restore(local_servers.card_url)

    login_queries = [query for path, query in state.requests if path == "/publicdir/sendurl"]
    assert [query["password"][0] for query in login_queries] == ["admin", NEW_ADMIN_PASSWORD]
    assert "password=Ad%20min_1" in state.request_targets[2]
    assert state.applied_settings == state.saved_settings
    assert state.reset_count == 1


def test_rejected_configuration_does_not_restart_card(local_servers: _LocalServers) -> None:
    state = local_servers.card_state
    state.config_result = "rejected"

    with pytest.raises(RestoreError):
        _restore(local_servers.card_url)

    assert state.saved_settings is not None
    assert state.reset_count == 0
    assert state.requests[-1][0] == "/config"
    assert state.requests[-1][1].get("reset") is None


@pytest.mark.parametrize("failure", ["http_error", "redirect"])
def test_transport_failures_do_not_leak_credentials_or_follow_redirects(
    local_servers: _LocalServers, failure: str
) -> None:
    base_url = local_servers.card_url
    state = local_servers.card_state
    state.config_result = failure
    state.redirect_target = f"{local_servers.redirect_url}/capture"

    with pytest.raises(RestoreError) as raised:
        _restore(base_url)

    message = str(raised.value)
    for secret in (NEW_SSID, NEW_WIFI_PASSWORD, NEW_ADMIN_PASSWORD, base_url):
        assert secret not in message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert state.reset_count == 0
    if failure == "redirect":
        assert local_servers.redirect_server.requests == []


if __name__ == "__main__":
    pytest_bazel.main()
