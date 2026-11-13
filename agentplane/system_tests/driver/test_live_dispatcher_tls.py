"""Lifecycle regressions for the live Dispatcher TLS helper process."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.driver.live_dispatcher_tls import verified_dispatcher_proxy


def _executable(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "dispatcher-proxy"
    script.write_text(f"#!/bin/sh\nset -eu\n{body}")
    script.chmod(0o700)
    return script


async def test_verified_dispatcher_proxy_reports_startup_failure(tmp_path: Path) -> None:
    binary = _executable(tmp_path, "printf 'proxy failed to start\\n' >&2\nexit 7\n")

    with pytest.raises(RuntimeError, match="proxy failed to start"):
        async with verified_dispatcher_proxy(binary, target="127.0.0.1:8080", ca_file=tmp_path / "ca.pem"):
            pytest.fail("failed helper unexpectedly reported readiness")


async def test_verified_dispatcher_proxy_stops_process_when_cancelled(tmp_path: Path) -> None:
    stopped = tmp_path / "stopped"
    ready = json.dumps(
        {"address": "127.0.0.1:45873", "root_certificates": "-----BEGIN CERTIFICATE-----\\n"}, separators=(",", ":")
    )
    binary = _executable(
        tmp_path,
        f"printf '%s\\n' '{ready}'\ntrap 'touch {shlex.quote(str(stopped))}; exit 0' TERM\nwhile :; do sleep 0.1; done\n",
    )
    entered = asyncio.Event()

    async def hold_proxy() -> None:
        async with verified_dispatcher_proxy(binary, target="127.0.0.1:8080", ca_file=tmp_path / "ca.pem"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_proxy())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.exists(), "helper process did not handle termination during fixture cancellation"


if __name__ == "__main__":
    pytest_bazel.main()
