"""Per-user D-Bus service for the live AIQuota view."""

import asyncio
import contextlib
import logging
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method, signal as dbus_signal
from httpx_sse import ServerSentEvent, aconnect_sse
from pydantic import ValidationError

from aiquota.cache import QuotaCache
from aiquota.config import DEFAULT_CONFIG_PATH, Config, load as load_config
from aiquota.models import AllQuotas
from aiquota.render.view_model import AllQuotasView, to_view
from util.network_monitor import NetworkMonitor

BUS_NAME = "works.allegedly.AiQuota"
OBJECT_PATH = "/works/allegedly/AiQuota"
INTERFACE_NAME = "works.allegedly.AiQuota1"
EVENTS_PATH = "/v1/events"
BASE_RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 60.0
READ_TIMEOUT_SECONDS = 45.0

if TYPE_CHECKING:
    DBusString = str
    DBusDouble = float
else:
    # dbus-next 0.2.3 inspects runtime annotations for literal wire signatures;
    # type checkers need the alias to be `str` instead.
    DBusString = "s"
    DBusDouble = "d"

logger = logging.getLogger(__name__)


class SSEProtocolError(RuntimeError):
    """The event endpoint did not provide a valid view stream."""


def parse_view_event(event: ServerSentEvent) -> AllQuotas | None:
    if event.event != "view":
        return None
    try:
        view = AllQuotasView.model_validate_json(event.data)
        # The server view extends raw quotas with backend-owned policy fields.
        # Pydantic reads the shared attributes and ignores those derived fields.
        return AllQuotas.model_validate(view, from_attributes=True)
    except ValidationError as exc:
        raise SSEProtocolError("AIQuota sent an invalid view event") from exc


class AiQuotaBusInterface(ServiceInterface):
    def __init__(self, owner: QuotaDaemon) -> None:
        super().__init__(INTERFACE_NAME)
        self.owner = owner

    @method(name="GetView")
    def get_view(self) -> DBusString:
        return self.owner.view_json

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def status(self) -> DBusString:
        return self.owner.status

    @dbus_property(access=PropertyAccess.READ, name="Error")
    def error(self) -> DBusString:
        return self.owner.error

    @dbus_property(access=PropertyAccess.READ, name="LastAttemptAt")
    def last_attempt_at(self) -> DBusDouble:
        return self.owner.last_attempt_at

    @dbus_property(access=PropertyAccess.READ, name="NextRetryAt")
    def next_retry_at(self) -> DBusDouble:
        return self.owner.next_retry_at

    @dbus_signal(name="ViewChanged")
    def view_changed(self, view_json: DBusString) -> DBusString:
        return view_json


class QuotaDaemon:
    def __init__(
        self,
        config: Config,
        cache: QuotaCache | None = None,
        http: httpx.AsyncClient | None = None,
        config_error: str | None = None,
    ) -> None:
        self.config = config
        self.config_error = config_error
        self.cache = cache or QuotaCache()
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=READ_TIMEOUT_SECONDS, write=10, pool=10), follow_redirects=False
        )
        cached = self.cache.read()
        self.view_json = to_view(cached).model_dump_json() if cached is not None else ""
        self.status = "starting"
        self.error = ""
        self.retry_delay = BASE_RETRY_SECONDS
        self.last_attempt_at = 0.0
        self.next_retry_at = 0.0
        self.stop_event = asyncio.Event()
        self.network_online_event = asyncio.Event()
        self.bus: MessageBus | None = None
        self.bus_interface = AiQuotaBusInterface(self)

    async def connect_bus(self) -> None:
        self.bus = await MessageBus().connect()
        self.bus.export(OBJECT_PATH, self.bus_interface)
        await self.bus.request_name(BUS_NAME)

    def set_status(self, status: str, error: str = "") -> None:
        changed: dict[str, str] = {}
        if self.status != status:
            self.status = status
            changed["Status"] = status
        if self.error != error:
            self.error = error
            changed["Error"] = error
        if changed:
            self.bus_interface.emit_properties_changed(changed)

    def accept_event(self, event: ServerSentEvent) -> bool:
        parsed = parse_view_event(event)
        if parsed is None:
            return False
        self.cache.write(parsed)
        # Keep the validated wire payload intact, including all server-derived fields.
        self.view_json = event.data
        self.bus_interface.view_changed(self.view_json)
        self.retry_delay = BASE_RETRY_SECONDS
        self.set_status("connected")
        return True

    def _set_attempt(self) -> None:
        self.last_attempt_at = time.time()
        changed: dict[str, float] = {"LastAttemptAt": self.last_attempt_at}
        if self.next_retry_at != 0:
            self.next_retry_at = 0.0
            changed["NextRetryAt"] = self.next_retry_at
        self.bus_interface.emit_properties_changed(changed)

    def _set_next_retry(self, retry_at: float | None) -> None:
        value = retry_at or 0.0
        if self.next_retry_at == value:
            return
        self.next_retry_at = value
        self.bus_interface.emit_properties_changed({"NextRetryAt": value})

    async def _await_connection_or_interrupt(self) -> None:
        connection = asyncio.create_task(self.connect_once())
        stopped = asyncio.create_task(self.stop_event.wait())
        recovered = asyncio.create_task(self.network_online_event.wait())
        try:
            await asyncio.wait((connection, stopped, recovered), return_when=asyncio.FIRST_COMPLETED)
            # Shutdown wins if it races a network transition or a connection error.
            if self.stop_event.is_set():
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
                return
            if self.network_online_event.is_set() and not connection.done():
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
                return
            await connection
        finally:
            for task in (connection, stopped, recovered):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connection, stopped, recovered, return_exceptions=True)

    async def _wait_for_retry(self, delay: float) -> None:
        stopped = asyncio.create_task(self.stop_event.wait())
        recovered = asyncio.create_task(self.network_online_event.wait())
        try:
            await asyncio.wait((stopped, recovered), timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopped, recovered):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stopped, recovered, return_exceptions=True)

    async def connect_once(self) -> None:
        url = self.config.remote_api.url
        token = self.config.remote_api.bearer_token.get_secret_value() if self.config.remote_api.bearer_token else None
        if not url:
            raise SSEProtocolError("AIQuota remote API URL is not configured")
        if not token:
            raise SSEProtocolError("AIQuota remote API bearer token is not configured")

        timeout = httpx.Timeout(connect=10, read=READ_TIMEOUT_SECONDS, write=10, pool=10)
        async with aconnect_sse(
            self.http,
            "GET",
            f"{url.rstrip('/')}{EVENTS_PATH}",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            timeout=timeout,
        ) as source:
            response = source.response
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise SSEProtocolError("AIQuota event endpoint did not return text/event-stream")
            async for event in source.aiter_sse():
                self.accept_event(event)
        if not self.stop_event.is_set():
            raise SSEProtocolError("AIQuota event stream ended")

    async def stream_forever(self) -> None:
        try:
            self.set_status("connecting")
            if self.config_error:
                self.set_status("disconnected", self.config_error)
                await self.stop_event.wait()
                return
            if not self.config.remote_api.url:
                self.set_status("disconnected", "AIQuota remote API URL is not configured")
                await self.stop_event.wait()
                return
            if not self.config.remote_api.bearer_token:
                self.set_status("disconnected", "AIQuota remote API bearer token is not configured")
                await self.stop_event.wait()
                return
            async with NetworkMonitor(on_online=self.network_online_event.set):
                while not self.stop_event.is_set():
                    # Clearing an earlier transition is safe because this new attempt
                    # itself acts on that recovery; later transitions still interrupt it.
                    self.network_online_event.clear()
                    self.set_status("connecting")
                    self._set_attempt()
                    try:
                        await self._await_connection_or_interrupt()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        message = _bounded_error(
                            exc,
                            self.config.remote_api.bearer_token.get_secret_value()
                            if self.config.remote_api.bearer_token
                            else None,
                        )
                        logger.warning("AIQuota event stream disconnected: %s", message)
                        self.set_status("disconnected", message)
                        if self.stop_event.is_set():
                            return
                        if self.network_online_event.is_set():
                            continue
                        self.network_online_event.clear()
                        delay = self.retry_delay
                        self._set_next_retry(time.time() + delay)
                        await self._wait_for_retry(delay)
                        self._set_next_retry(None)
                        if self.stop_event.is_set():
                            return
                        if self.network_online_event.is_set():
                            self.network_online_event.clear()
                            continue
                        self.retry_delay = min(self.retry_delay * 2, MAX_RETRY_SECONDS)
                    if self.stop_event.is_set():
                        return
        finally:
            self._set_next_retry(None)

    async def serve(self) -> None:
        stream_task: asyncio.Task[None] | None = None
        try:
            await self.connect_bus()
            self.set_status("connecting")
            stream_task = asyncio.create_task(self.stream_forever())
            await self.stop_event.wait()
        finally:
            self._set_next_retry(None)
            if stream_task is not None:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            if self.bus is not None:
                self.bus.disconnect()
            await self.http.aclose()


def _bounded_error(exc: Exception, token: str | None) -> str:
    message = " ".join(str(exc).splitlines())
    if token:
        message = message.replace(token, "[redacted]")
    return (message or type(exc).__name__)[:400]


def _load_config(path: Path = DEFAULT_CONFIG_PATH) -> tuple[Config, str | None]:
    try:
        return load_config(path), None
    except FileNotFoundError:
        return Config(), "AIQuota configuration file is missing"
    except OSError, ValueError:
        # Parse errors can include source text; never send that through D-Bus or logs.
        return Config(), "AIQuota configuration could not be loaded"


async def run() -> None:
    config, config_error = _load_config()
    daemon = QuotaDaemon(config, config_error=config_error)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, daemon.stop_event.set)
    await daemon.serve()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
