from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from devinfra.js.debundle.live_proxy import server
from devinfra.js.debundle.live_proxy.core import LiveProxyOptions


@dataclass
class _FakeConfig:
    state_dir: Path
    ca_dir: Path
    proxy_host: str = "127.0.0.1"
    # A port nothing is listening on, so `wait_for_port` never succeeds and
    # the proxy task's failure is what must surface.
    proxy_port: int = 65432


class _FakeAddons:
    def add(self, *_args, **_kwargs) -> None:
        pass


class _FailingMaster:
    """Stand-in DumpMaster whose run() fails immediately, the way a real
    listen-port bind failure would."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.addons = _FakeAddons()

    async def run(self) -> None:
        raise RuntimeError("listen port bind failed")

    def shutdown(self) -> None:
        pass


class StartProxyInProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_propagates_proxy_startup_failure(self) -> None:
        # A proxy whose run() fails during startup must surface that error to
        # the caller, not swallow it (as "Task exception was never retrieved")
        # behind a generic wait_for_port timeout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _FakeConfig(state_dir=root / "state", ca_dir=root / "ca")
            with (
                mock.patch.object(server, "load_live_proxy_configuration", return_value=config),
                mock.patch.object(server, "DumpMaster", _FailingMaster),
                mock.patch.object(server, "DebundleLiveProxyAddon"),
                mock.patch.object(server, "Options"),
                pytest.raises(RuntimeError) as excinfo,
            ):
                async with server.start_proxy_in_process(LiveProxyOptions()):
                    pass
        assert "listen port bind failed" in str(excinfo.value)


if __name__ == "__main__":
    unittest.main()
