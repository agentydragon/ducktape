"""mitmproxy hosted in-process, with the options the fail-closed contract depends on pinned in code.

The addon is an ordinary mitmproxy addon and would load under `mitmdump -s` too; hosting the master
here is what lets one Python binary own the listener, the informer, and the admin server, and what
keeps `connection_strategy=lazy` — without it mitmproxy dials the upstream before the gate runs — a
fact of the code rather than of a command line.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

from mitmproxy import addons
from mitmproxy.master import Master
from mitmproxy.options import Options

logger = logging.getLogger(__name__)

CA_BASENAME = "mitmproxy-ca.pem"

# A master that neither signals startup nor exits, and one that will not stop after `shutdown`, are
# both bugs rather than slow machines -- mitmproxy does each in milliseconds. Bounding them is what
# makes such a master say which half of its lifecycle hung: unbounded, the process simply sits
# there until something outside it loses patience, which reads as a slow test rather than a stuck
# proxy and hides the cause behind whatever timeout finally fires.
STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0


class _RunningSignal:
    """Addon signalling that the master finished startup (listeners bound)."""

    def __init__(self) -> None:
        self.running_event = asyncio.Event()

    def running(self) -> None:
        self.running_event.set()


def write_interception_ca(confdir: Path, ca_cert: Path, ca_key: Path) -> None:
    """Assemble the one file mitmproxy reads its CA from: the key and the certificate, PEM, in `confdir`."""
    confdir.mkdir(parents=True, exist_ok=True)
    target = confdir / CA_BASENAME
    target.touch(mode=0o600)
    target.write_bytes(ca_key.read_bytes() + b"\n" + ca_cert.read_bytes())


class EgressProxyServer:
    """One listener whose flows `addon` gates. Async context manager: entering starts the master and
    waits until the listener is bound (`listen_port=0` picks an ephemeral one, then read
    `listen_port`); exiting shuts it down."""

    def __init__(
        self,
        addon: object,
        *,
        confdir: Path,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        extra_options: Mapping[str, object] | None = None,
    ) -> None:
        self._addon = addon
        self._confdir = confdir
        self._listen_host = listen_host
        self._listen_port = listen_port
        # Applied before the pinned options below, which always win: nothing passed here can weaken
        # the gate. The tests use it to trust their throwaway upstream CA.
        self._extra_options = dict(extra_options or {})
        self._master: Master | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._bound_port: int | None = None

    async def __aenter__(self) -> Self:
        master = Master(
            Options(listen_host=self._listen_host, listen_port=self._listen_port, confdir=str(self._confdir))
        )
        master.addons.add(*addons.default_addons())
        signal = _RunningSignal()
        master.addons.add(self._addon, signal)
        if self._extra_options:
            master.options.update(**self._extra_options)
        # lazy: the eager default dials the upstream before the gate's hook runs. The onboarding app
        # (mit.it) is an ungated response surface nothing here needs.
        master.options.update(connection_strategy="lazy", onboarding=False)
        self._master = master
        self._run_task = asyncio.create_task(master.run(), name="egress-proxy-master")
        try:
            running = asyncio.create_task(signal.running_event.wait())
            done, _pending = await asyncio.wait(
                {self._run_task, running}, timeout=STARTUP_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                running.cancel()
                raise TimeoutError(
                    f"mitmproxy master neither finished startup nor exited within {STARTUP_TIMEOUT_SECONDS}s"
                )
            if running not in done:
                running.cancel()
                self._run_task.result()  # startup failed: surface the exception
                raise RuntimeError("mitmproxy master exited before startup completed")
            listen_addrs = master.addons.get("proxyserver").listen_addrs()
            if not listen_addrs:
                # A bind failure does not stop the master; it only leaves it without a listener.
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
            await asyncio.wait_for(self._run_task, SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            # `wait_for` has already cancelled the task and waited for it to unwind, so the port is
            # not left bound; raising here names the proxy rather than whatever fails after it.
            raise TimeoutError(f"mitmproxy master did not stop within {SHUTDOWN_TIMEOUT_SECONDS}s") from None
        finally:
            self._bound_port = None
