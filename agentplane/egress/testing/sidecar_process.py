"""Launch and own the built Go egress sidecar in Python end-to-end tests."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack, suppress
from pathlib import Path
from types import MappingProxyType

import aiohttp
import yaml
from python.runfiles import Runfiles

from agentplane.egress.platform_routes import PLATFORM_ROUTES_LISTEN_ADDRESS, PlatformRoute, PlatformRoutes
from agentplane.egress.sidecar_config import Proxy, Settings
from util.net import Listener, listen_tcp

_BINARY = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/egress/sidecar/egress-sidecar_/egress-sidecar"


class SidecarProcess:
    def __init__(
        self,
        *,
        proxy: Proxy,
        audience_token_files: Mapping[str, Path] = MappingProxyType({}),
        platform_routes: Mapping[str, PlatformRoute] = MappingProxyType({}),
    ) -> None:
        self._proxy = proxy
        self._audience_token_files = audience_token_files
        self._platform_routes = platform_routes
        self._listener_stack = ExitStack()
        self._proxy_listener: Listener | None = None
        self._readiness_listener: Listener | None = None
        self._platform_route_listener: Listener | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self._settings_dir = tempfile.TemporaryDirectory()

    async def __aenter__(self) -> SidecarProcess:
        try:
            self._proxy_listener = self._listener_stack.enter_context(listen_tcp())
            self._readiness_listener = self._listener_stack.enter_context(listen_tcp())
            routes = self._platform_routes
            if routes:
                self._platform_route_listener = self._listener_stack.enter_context(listen_tcp())
                routes_config = PlatformRoutes(
                    listen_address=self._fd_address(self._platform_route_listener), routes=dict(routes)
                )
            else:
                routes_config = PlatformRoutes(listen_address=PLATFORM_ROUTES_LISTEN_ADDRESS)

            settings = Settings(
                proxy=self._proxy,
                audience_token_files=dict(self._audience_token_files),
                platform_routes=routes_config,
                listen_address=self._fd_address(self.proxy_listener),
                readiness_listen_address=self._fd_address(self.readiness_listener),
            )
            settings_path = Path(self._settings_dir.name) / "settings.yaml"
            settings_path.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True)))
            env = {Settings.config_file_env: str(settings_path)}
            listeners = [self.proxy_listener, self.readiness_listener]
            if self._platform_route_listener is not None:
                listeners.append(self._platform_route_listener)
            pass_fds = tuple(listener.socket.fileno() for listener in listeners)
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    _BINARY,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    pass_fds=pass_fds,
                )
            )
            cancelled = False
            while True:
                try:
                    self.process = await asyncio.shield(spawn)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if spawn.done():
                        self.process = spawn.result()
                        break
            # The child now owns the inherited descriptors. Keep the cached
            # host/port values for clients, but release the parent's copies.
            self._listener_stack.close()
            if cancelled:
                raise asyncio.CancelledError
            assert self.process.stderr is not None
            self._stderr_task = asyncio.create_task(self._capture_stderr(self.process.stderr))
            await self._wait_ready()
            return self
        except BaseException:
            await self._stop()
            raise

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._stop()

    @property
    def proxy_listener(self) -> Listener:
        if self._proxy_listener is None:
            raise RuntimeError("sidecar proxy listener has not been allocated")
        return self._proxy_listener

    @property
    def readiness_listener(self) -> Listener:
        if self._readiness_listener is None:
            raise RuntimeError("sidecar readiness listener has not been allocated")
        return self._readiness_listener

    @property
    def platform_route_listener(self) -> Listener:
        if self._platform_route_listener is None:
            raise RuntimeError("sidecar platform route listener has not been allocated")
        return self._platform_route_listener

    @staticmethod
    def _fd_address(listener: Listener) -> str:
        return f"fd:{listener.socket.fileno()}"

    async def _capture_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def _wait_ready(self) -> None:
        assert self.process is not None
        deadline = asyncio.get_running_loop().time() + 10
        listener = self.readiness_listener
        async with aiohttp.ClientSession() as session:
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    raise RuntimeError(f"Go sidecar exited with {self.process.returncode}: {self._logs()}")
                try:
                    async with session.get(
                        f"http://{listener.host}:{listener.port}/readyz", timeout=aiohttp.ClientTimeout(total=0.5)
                    ) as response:
                        if response.status == 200:
                            return
                except aiohttp.ClientError, TimeoutError:
                    pass
                await asyncio.sleep(0.02)
        raise TimeoutError(f"Go sidecar did not become ready: {self._logs()}")

    async def _stop(self) -> None:
        try:
            await self._stop_process()
        finally:
            self._listener_stack.close()
            self._settings_dir.cleanup()

    async def _stop_process(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        returncode = self.process.returncode
        self.process = None
        if returncode != 0:
            raise RuntimeError(f"Go sidecar exited with {returncode}: {self._logs()}")

    def _logs(self) -> str:
        return "\n".join(self._stderr)
