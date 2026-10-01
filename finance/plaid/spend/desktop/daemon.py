"""Per-user D-Bus daemon that streams the current Plaid spend view."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from typing import Any

import httpx
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method, signal as dbus_signal

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

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class PlaidSpendBusInterface(ServiceInterface):
    def __init__(self, owner: Any) -> None:
        super().__init__(INTERFACE_NAME)
        self.owner = owner

    # dbus-next uses these string annotations as the wire signatures. They must
    # stay quoted and this file intentionally avoids postponed annotations.
    @method()
    def GetView(self) -> "s":  # type: ignore[name-defined]  # noqa: N802, F821, UP037
        return json.dumps(self.owner.view, separators=(",", ":"))

    @method()
    def Login(self):  # noqa: N802
        self.owner.start_login()

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":  # type: ignore[name-defined]  # noqa: N802, F821, UP037
        return self.owner.status

    @dbus_property(access=PropertyAccess.READ)
    def LastError(self) -> "s":  # type: ignore[name-defined]  # noqa: N802, F821, UP037
        return self.owner.error or ""

    @dbus_signal()
    def ViewChanged(self, view: "s") -> "s":  # type: ignore[name-defined]  # noqa: N802, F821, UP037
        return view

    def update_properties(self, status: str | None = None, error: str | None = None) -> None:
        changed: dict[str, str] = {}
        if status is not None:
            changed["Status"] = status
        if error is not None:
            changed["LastError"] = error
        if changed:
            self.emit_properties_changed(changed)


class PlaidSpendDaemon:
    def __init__(self) -> None:
        self.view: dict[str, Any] = {"generated_at": None, "cards": []}
        self.status = "starting"
        self.error = ""
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
        try:
            await self.auth.login()
            self._set_status("connecting", "")
            self._ensure_stream_task()
        except Exception as exc:  # report a bounded message through D-Bus, not credentials
            message = str(exc).replace("\n", " ")[:400]
            logger.warning("Plaid Spend sign-in failed: %s", message)
            self._set_status("authentication-required", message)

    def _ensure_stream_task(self) -> None:
        if self.stream_task is None or self.stream_task.done():
            self.stream_task = asyncio.create_task(self._stream_forever())

    def _set_status(self, status: str, error: str) -> None:
        status_changed = status != self.status
        error_changed = error != self.error
        self.status = status
        self.error = error
        self.bus_interface.update_properties(
            status=status if status_changed else None, error=error if error_changed else None
        )

    def _set_view(self, view: dict[str, Any]) -> None:
        self.view = view
        self.bus_interface.ViewChanged(json.dumps(view, separators=(",", ":")))

    async def _stream_forever(self) -> None:
        delay = 2.0
        while not self.shutting_down:
            try:
                token = await self.auth.access_token_for_api()
                view = await self._get_view(token)
                self._set_view(view)
                self._set_status("ready", "")
                await self._consume_events()
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

            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _get_view(self, token: str) -> dict[str, Any]:
        response = await self.http.get(f"{API_URL}/api/v1/view", headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            token = await self.auth.access_token_for_api(force_refresh=True)
            response = await self.http.get(f"{API_URL}/api/v1/view", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        view = response.json()
        if not isinstance(view, dict) or not isinstance(view.get("cards"), list):
            raise RuntimeError("Plaid Spend returned an invalid view; expected an object with a cards array")
        return view

    async def _consume_events(self) -> None:
        token = await self.auth.access_token_for_api()
        response_context = self.http.stream(
            "GET",
            f"{API_URL}/api/v1/events",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        async with response_context as response:
            if response.status_code == 401:
                await self.auth.access_token_for_api(force_refresh=True)
                return
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
                    return
                except StopAsyncIteration:
                    return

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

    async def close(self) -> None:
        self.shutting_down = True
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

    if await daemon.token_store.load_refresh_token():
        daemon._set_status("connecting", "")
        daemon._ensure_stream_task()
    else:
        daemon._set_status("authentication-required", "Sign in to connect this computer")

    try:
        await stop.wait()
    finally:
        await daemon.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
