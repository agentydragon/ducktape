"""Per-user D-Bus daemon that streams the current Plaid spend view."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from typing import TYPE_CHECKING, Any

import httpx
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method, signal as dbus_signal

from util.network_monitor import NetworkMonitor

from .credentials import SecretServiceTokenStore
from .oauth import AuthenticationRequiredError, Authenticator, OAuthConfig

BUS_NAME = "works.allegedly.PlaidSpend"
OBJECT_PATH = "/works/allegedly/PlaidSpend"
INTERFACE_NAME = "works.allegedly.PlaidSpend1"
API_URL = os.environ.get("PLAID_SPEND_API_URL", "https://plaid-spend.allegedly.works").rstrip("/")
OIDC_ISSUER = os.environ.get(
    "PLAID_SPEND_OIDC_ISSUER", "https://auth.allegedly.works/application/o/plaid-spend-desktop/"
)
OIDC_CLIENT_ID = os.environ.get("PLAID_SPEND_OIDC_CLIENT_ID", "plaid-spend-desktop")

if TYPE_CHECKING:
    DBusString = str
    DBusDouble = float
else:
    # dbus-next inspects runtime annotations for literal wire signatures.
    DBusString = "s"
    DBusDouble = "d"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class PlaidSpendBusInterface(ServiceInterface):
    def __init__(self, owner: PlaidSpendDaemon) -> None:
        super().__init__(INTERFACE_NAME)
        self.owner = owner

    @method(name="GetView")
    def get_view(self) -> DBusString:
        return json.dumps(self.owner.view, separators=(",", ":"))

    @method(name="Login")
    def login(self) -> None:
        self.owner.start_login()

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def status(self) -> DBusString:
        return self.owner.status

    @dbus_property(access=PropertyAccess.READ, name="LastError")
    def last_error(self) -> DBusString:
        return self.owner.error or ""

    @dbus_property(access=PropertyAccess.READ, name="LastAttemptAt")
    def last_attempt_at(self) -> DBusDouble:
        return self.owner.last_attempt_at

    @dbus_property(access=PropertyAccess.READ, name="NextRetryAt")
    def next_retry_at(self) -> DBusDouble:
        return self.owner.next_retry_at

    @dbus_signal(name="ViewChanged")
    def view_changed(self, view: DBusString) -> DBusString:
        return view

    def update_properties(
        self,
        status: str | None = None,
        error: str | None = None,
        last_attempt_at: float | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        changed: dict[str, Any] = {}
        if status is not None:
            changed["Status"] = status
        if error is not None:
            changed["LastError"] = error
        if last_attempt_at is not None:
            changed["LastAttemptAt"] = last_attempt_at
        if next_retry_at is not None:
            changed["NextRetryAt"] = next_retry_at
        if changed:
            self.emit_properties_changed(changed)


class PlaidSpendDaemon:
    def __init__(self) -> None:
        self.view: dict[str, Any] = {"generated_at": None, "cards": []}
        self.status = "starting"
        self.error = ""
        self.last_attempt_at = 0.0
        self.next_retry_at = 0.0
        self.token_store = SecretServiceTokenStore()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=None, write=15, pool=15), follow_redirects=False
        )
        self.auth = Authenticator(
            OAuthConfig(issuer=OIDC_ISSUER, client_id=OIDC_CLIENT_ID), self.http, self.token_store
        )
        self.bus_interface = PlaidSpendBusInterface(self)
        self.bus: MessageBus | None = None
        self.stream_task: asyncio.Task[None] | None = None
        self.login_task: asyncio.Task[None] | None = None
        self._retry_delay = 2.0
        self._network_wake = asyncio.Event()
        self.shutting_down = False

    async def connect_bus(self) -> None:
        bus = await MessageBus().connect()
        self.bus = bus
        bus.export(OBJECT_PATH, self.bus_interface)
        await bus.request_name(BUS_NAME)

    def start_login(self) -> None:
        if self.login_task and not self.login_task.done():
            return
        self.login_task = asyncio.create_task(self._login())

    async def _login(self) -> None:
        self._set_status("authorizing", "")
        self._set_next_retry_at(0.0)
        self._network_wake.clear()
        stream_task = self.stream_task
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
        if self.stream_task is stream_task:
            self.stream_task = None
        try:
            await self.auth.login()
            self._set_status("connecting", "")
            self._ensure_stream_task()
        except Exception as exc:  # report a bounded message through D-Bus, not credentials
            message = str(exc).replace("\n", " ")[:400]
            logger.warning("Plaid Spend sign-in failed: %s", message)
            self._set_status("authentication-required", message)

    def _ensure_stream_task(self) -> None:
        if self.shutting_down or self.status in ("authentication-required", "authorizing"):
            return
        if self.stream_task is None or self.stream_task.done():
            self.stream_task = asyncio.create_task(self._stream_forever())

    def network_online(self) -> None:
        """Wake a pending retry or interrupt a stream that stalled while offline."""
        self._network_wake.set()

    def _set_status(self, status: str, error: str) -> None:
        status_changed = status != self.status
        error_changed = error != self.error
        self.status = status
        self.error = error
        if status in ("authentication-required", "authorizing"):
            self._set_next_retry_at(0.0)
        self.bus_interface.update_properties(
            status=status if status_changed else None, error=error if error_changed else None
        )

    def _set_last_attempt_at(self, timestamp: float) -> None:
        if timestamp == self.last_attempt_at:
            return
        self.last_attempt_at = timestamp
        self.bus_interface.update_properties(last_attempt_at=timestamp)

    def _set_next_retry_at(self, timestamp: float) -> None:
        if timestamp == self.next_retry_at:
            return
        self.next_retry_at = timestamp
        self.bus_interface.update_properties(next_retry_at=timestamp)

    def _set_view(self, view: dict[str, Any]) -> None:
        self.view = view
        self._retry_delay = 2.0
        self.bus_interface.view_changed(json.dumps(view, separators=(",", ":")))

    async def _stream_forever(self) -> None:
        try:
            while not self.shutting_down:
                if self.status in ("authentication-required", "authorizing"):
                    return
                self._network_wake.clear()
                self._set_status("connecting", "")
                self._set_last_attempt_at(time.time())
                self._set_next_retry_at(0.0)
                try:
                    if await self._consume_or_network_wake():
                        continue
                    if self.shutting_down:
                        return
                    self._set_status("connecting", "The event stream closed; reconnecting")
                except AuthenticationRequiredError as exc:
                    self._set_status("authentication-required", str(exc))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc).replace("\n", " ")[:400]
                    logger.warning("Plaid Spend connection failed: %s", message)
                    self._set_status("error", message)

                if self.shutting_down or self.status in ("authentication-required", "authorizing"):
                    return
                delay = self._retry_delay
                self._set_next_retry_at(time.time() + delay)
                if await self._wait_for_retry_or_network(delay):
                    if self.shutting_down:
                        return
                    continue
                self._retry_delay = min(self._retry_delay * 2, 60.0)
        finally:
            self._set_next_retry_at(0.0)

    async def _consume_or_network_wake(self) -> bool:
        """Return when the stream ends or network recovery cancels its current connection."""
        consume_task = asyncio.create_task(self._consume_events())
        wake_task = asyncio.create_task(self._network_wake.wait())
        try:
            done, _ = await asyncio.wait((consume_task, wake_task), return_when=asyncio.FIRST_COMPLETED)
            if consume_task in done:
                result = consume_task.result()
                if wake_task in done:
                    self._network_wake.clear()
                    return True
                return result

            self._network_wake.clear()
            self._set_status("connecting", "Network is back; reconnecting")
            consume_task.cancel()
            await asyncio.gather(consume_task, return_exceptions=True)
            return True
        finally:
            if not wake_task.done():
                wake_task.cancel()
            if not consume_task.done():
                consume_task.cancel()
            await asyncio.gather(consume_task, wake_task, return_exceptions=True)

    async def _wait_for_retry_or_network(self, delay: float) -> bool:
        """Return true when network recovery interrupts the scheduled retry delay."""
        try:
            await asyncio.wait_for(self._network_wake.wait(), timeout=delay)
        except TimeoutError:
            return False
        self._network_wake.clear()
        return True

    async def _consume_events(self) -> bool:
        """Consume one stream; return true when a refreshed token should reconnect immediately."""
        token = await self.auth.access_token_for_api()
        response_context = self.http.stream(
            "GET",
            f"{API_URL}/api/v1/events",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        async with response_context as response:
            if response.status_code == 401:
                await self.auth.access_token_for_api(force_refresh=True)
                return True
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise RuntimeError("Plaid Spend event endpoint did not return text/event-stream")

            event_name = "message"
            data_lines: list[str] = []
            iterator = response.aiter_lines().__aiter__()
            deadline = self.auth.access_token_expires_at - 30
            while not self.shutting_down:
                timeout = max(1.0, deadline - time.monotonic())
                try:
                    line = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except TimeoutError:
                    # Reconnect before the bearer expires; the next connection obtains
                    # a fresh access token, including via a rotated refresh token.
                    return True
                except StopAsyncIteration:
                    return False

                if not line:
                    if data_lines and event_name in ("view", "message"):
                        payload = json.loads("\n".join(data_lines))
                        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
                            raise RuntimeError("Plaid Spend sent an invalid view event")
                        self._set_view(payload)
                        self._set_status("ready", "")
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator:
                    value = ""
                elif value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
        return False

    async def close(self) -> None:
        self.shutting_down = True
        self._set_next_retry_at(0.0)
        tasks = [task for task in (self.stream_task, self.login_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.bus is not None:
            self.bus.disconnect()
        await self.http.aclose()


async def run() -> None:
    daemon = PlaidSpendDaemon()
    await daemon.connect_bus()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    try:
        async with NetworkMonitor(on_online=daemon.network_online):
            if await daemon.token_store.load_refresh_token():
                daemon._set_status("connecting", "")
                daemon._ensure_stream_task()
            else:
                daemon._set_status("authentication-required", "Sign in to connect this computer")
            await stop.wait()
    finally:
        await daemon.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
