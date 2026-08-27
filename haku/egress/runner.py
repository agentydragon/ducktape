"""Embedded mitmproxy runner: the egress proxy as a library, not a mitmdump script.

Owning the master in-process is what makes fail-closed semantics ours
(github.com/agentydragon/ducktape/issues/4670): the gate addon (see addon.py)
refuses flows on any decision-path failure, and this runner pins the mitmproxy
options that contract depends on. The proxy runs in mitmproxy's stock regular
mode — HTTP CONNECT tunnels with TLS MITM from the CA under ``confdir``;
distributing that CA to clients is deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import TracebackType
from typing import Self

from mitmproxy import addons
from mitmproxy.master import Master
from mitmproxy.options import Options

from haku.egress.addon import DEFAULT_DECIDE_TIMEOUT_SECONDS, EgressGateAddon
from haku.egress.decide_client import DecideClient

logger = logging.getLogger(__name__)


class _RunningSignal:
    """Addon signalling that the master finished startup (listeners bound)."""

    def __init__(self) -> None:
        self.running_event = asyncio.Event()

    def running(self) -> None:
        self.running_event.set()


class EgressProxy:
    """One in-process mitmproxy listener whose every flow is gated by ``decide``.

    Async context manager: entering starts the master and waits until the
    listener is bound (pass ``listen_port=0`` for an ephemeral port, then read
    ``listen_port``); exiting shuts the master down.
    """

    def __init__(
        self,
        decide: DecideClient,
        *,
        confdir: Path,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        decide_timeout_seconds: float = DEFAULT_DECIDE_TIMEOUT_SECONDS,
    ) -> None:
        self._decide = decide
        self._confdir = confdir
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._decide_timeout_seconds = decide_timeout_seconds
        self._master: Master | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._bound_port: int | None = None

    async def __aenter__(self) -> Self:
        master = Master(
            Options(listen_host=self._listen_host, listen_port=self._listen_port, confdir=str(self._confdir))
        )
        master.addons.add(*addons.default_addons())
        signal = _RunningSignal()
        master.addons.add(EgressGateAddon(self._decide, decide_timeout_seconds=self._decide_timeout_seconds), signal)
        # Addon-owned options exist only after registration. lazy: the default
        # eager strategy dials the upstream before the gate's request hook runs —
        # a fail-open leak. The onboarding app (mitm.it) is an ungated response
        # surface the fence does not need.
        master.options.update(connection_strategy="lazy", onboarding=False)
        self._master = master
        self._run_task = asyncio.create_task(master.run(), name="egress-proxy-master")
        try:
            running = asyncio.create_task(signal.running_event.wait())
            done, _pending = await asyncio.wait({self._run_task, running}, return_when=asyncio.FIRST_COMPLETED)
            if running not in done:
                running.cancel()
                self._run_task.result()  # startup failed: surface the exception
                raise RuntimeError("mitmproxy master exited before startup completed")
            listen_addrs = master.addons.get("proxyserver").listen_addrs()
            if not listen_addrs:
                # Bind failures do not stop the master (that is mitmdump's
                # errorcheck addon, which exits the whole process): they only
                # surface as a running master with no listener.
                raise RuntimeError("mitmproxy startup completed without a bound listener")
            self._bound_port = listen_addrs[0][1]
        except BaseException:
            await self._stop()
            raise
        logger.info("egress proxy listening on %s:%d", self._listen_host, self._bound_port)
        return self

    @property
    def listen_port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("egress proxy is not running")
        return self._bound_port

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stop()

    async def _stop(self) -> None:
        assert self._master is not None
        assert self._run_task is not None
        self._master.shutdown()
        try:
            await self._run_task
        finally:
            self._bound_port = None
