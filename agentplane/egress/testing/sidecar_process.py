"""Launch and own the built Go egress sidecar in Python end-to-end tests."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import aiohttp
import yaml

from agentplane.egress.sidecar_config import Settings
from agentplane.testing.go_process import TcpEndpoint, startup_object
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.settings_contract import settings_file

_BINARY = get_required_path(own_repo_rlocation("agentplane/egress/sidecar/egress-sidecar_/egress-sidecar"))
_STARTUP_FILE_ENV = "AGENTPLANE_EGRESS_SIDECAR_STARTUP_FILE"


@dataclass(frozen=True)
class SidecarEndpoints:
    listener: TcpEndpoint
    readiness: TcpEndpoint
    platform_routes: Mapping[str, TcpEndpoint]

    @classmethod
    def parse(cls, contents: bytes, *, expected_routes: set[str]) -> SidecarEndpoints:
        value = startup_object(contents)
        if value.keys() != {"listener", "readiness", "platform_routes"}:
            raise ValueError("sidecar startup endpoints must contain listener, readiness, and platform_routes")
        routes = value["platform_routes"]
        if not isinstance(routes, dict) or any(not isinstance(name, str) for name in routes):
            raise TypeError("sidecar platform route endpoints must be a JSON object with string keys")
        if routes.keys() != expected_routes:
            raise ValueError("sidecar platform route endpoint names do not match the configured routes")
        return cls(
            listener=TcpEndpoint.parse(value["listener"]),
            readiness=TcpEndpoint.parse(value["readiness"]),
            platform_routes={name: TcpEndpoint.parse(endpoint) for name, endpoint in routes.items()},
        )


class SidecarProcess:
    def __init__(
        self,
        *,
        proxy_host: str,
        proxy_port: int,
        token_file: Path,
        audience_token_files: Mapping[str, Path] = MappingProxyType({}),
        platform_routes: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        listen_host: str = "127.0.0.1",
        listen_port: int | None = None,
        readiness_host: str = "127.0.0.1",
        readiness_port: int | None = None,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._token_file = token_file
        self._audience_token_files = audience_token_files
        self._platform_routes = platform_routes
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._readiness_host = readiness_host
        self._readiness_port = readiness_port
        self.endpoints: SidecarEndpoints | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self._settings_dir = tempfile.TemporaryDirectory()

    async def __aenter__(self) -> SidecarProcess:
        try:
            routes = {name: dict(config) for name, config in self._platform_routes.items()}
            listen_port = 0 if self._listen_port is None else self._listen_port
            readiness_port = 0 if self._readiness_port is None else self._readiness_port
            for name, config in routes.items():
                if not isinstance(config.get("listen_port"), int):
                    raise TypeError(f"platform route {name!r} listen_port must be an integer")
            settings = settings_file(
                Settings,
                {
                    "proxy_host": self._proxy_host,
                    "proxy_port": self._proxy_port,
                    "token_file": str(self._token_file),
                    "audience_token_files": {
                        audience: str(path) for audience, path in self._audience_token_files.items()
                    },
                    "platform_routes": routes,
                    "listen_host": self._listen_host,
                    "listen_port": listen_port,
                    "readiness_host": self._readiness_host,
                    "readiness_port": readiness_port,
                },
            )
            private = Path(self._settings_dir.name)
            settings_path = private / "settings.yaml"
            startup_file = private / "startup.json"
            settings_path.write_text(yaml.safe_dump(settings))
            env = {Settings.config_file_env: str(settings_path), _STARTUP_FILE_ENV: str(startup_file)}
            self.process = await asyncio.create_subprocess_exec(
                str(_BINARY), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env
            )
            assert self.process.stderr is not None
            self._stderr_task = asyncio.create_task(self._capture_stderr(self.process.stderr))
            self.endpoints = await self._wait_for_endpoints(startup_file, expected_routes=set(routes))
            self._listen_port = self.endpoints.listener.port
            self._readiness_port = self.endpoints.readiness.port
            await self._wait_ready()
        except BaseException:
            await self._stop(check_exit=False)
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._stop(check_exit=exc_type is None)

    @property
    def listen_port(self) -> int:
        assert self._listen_port is not None
        return self._listen_port

    @property
    def readiness_port(self) -> int:
        assert self._readiness_port is not None
        return self._readiness_port

    @property
    def platform_route_ports(self) -> Mapping[str, int]:
        assert self.endpoints is not None
        return {name: endpoint.port for name, endpoint in self.endpoints.platform_routes.items()}

    async def _capture_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def _wait_ready(self) -> None:
        assert self.process is not None
        assert self.endpoints is not None
        deadline = asyncio.get_running_loop().time() + 10
        async with aiohttp.ClientSession() as session:
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    await self._drain_stderr()
                    raise RuntimeError(f"Go sidecar exited with {self.process.returncode}: {self._logs()}")
                try:
                    async with session.get(
                        f"http://{self.endpoints.readiness.url_host}:{self.readiness_port}/readyz",
                        timeout=aiohttp.ClientTimeout(total=0.5),
                    ) as response:
                        if response.status == 200:
                            return
                except aiohttp.ClientError, TimeoutError:
                    pass
                await asyncio.sleep(0.02)
        raise TimeoutError(f"Go sidecar did not become ready: {self._logs()}")

    async def _wait_for_endpoints(self, startup_file: Path, *, expected_routes: set[str]) -> SidecarEndpoints:
        assert self.process is not None
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if self.process.returncode is not None:
                await self._drain_stderr()
                raise RuntimeError(
                    f"Go sidecar exited with {self.process.returncode} before endpoint publication: {self._logs()}"
                )
            try:
                contents = await asyncio.to_thread(startup_file.read_bytes)
            except FileNotFoundError:
                await asyncio.sleep(0.02)
                continue
            return SidecarEndpoints.parse(contents, expected_routes=expected_routes)
        raise TimeoutError(f"Go sidecar did not publish endpoints: {self._logs()}")

    async def _drain_stderr(self) -> None:
        if self._stderr_task is not None:
            await self._stderr_task

    async def _stop(self, *, check_exit: bool) -> None:
        try:
            await self._stop_process(check_exit=check_exit)
        finally:
            self._settings_dir.cleanup()

    async def _stop_process(self, *, check_exit: bool) -> None:
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
        await self._drain_stderr()
        returncode = self.process.returncode
        self.process = None
        if check_exit and returncode != 0:
            raise RuntimeError(f"Go sidecar exited with {returncode}: {self._logs()}")

    def _logs(self) -> str:
        return "\n".join(self._stderr)
