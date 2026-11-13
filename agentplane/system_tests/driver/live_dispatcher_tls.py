"""Own the test-only TLS-terminating Dispatcher proxy process."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalDispatcherEndpoint:
    address: str
    root_certificates: bytes


@asynccontextmanager
async def verified_dispatcher_proxy(
    binary: Path, *, target: str, ca_file: Path
) -> AsyncIterator[LocalDispatcherEndpoint]:
    """Run a loopback proxy that verifies the Dispatcher CA and SPIFFE server URI."""
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--target",
        target,
        "--ca-file",
        str(ca_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: deque[str] = deque(maxlen=20)

    async def drain_stderr() -> None:
        assert process.stderr is not None
        async for line in process.stderr:
            stderr_tail.append(line.decode(errors="replace").rstrip())

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        except TimeoutError as error:
            raise RuntimeError("Dispatcher TLS proxy did not report readiness") from error
        if not line:
            return_code = await process.wait()
            await stderr_task
            detail = "\n".join(stderr_tail)
            raise RuntimeError(f"Dispatcher TLS proxy exited with {return_code}: {detail}")

        ready = json.loads(line)
        address = ready["address"]
        host, separator, port = address.rpartition(":")
        if not separator or not 1 <= int(port) <= 65535 or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError(f"Dispatcher TLS proxy reported a non-loopback address: {address!r}")
        root_certificates = ready["root_certificates"].encode()
        if b"-----BEGIN CERTIFICATE-----" not in root_certificates:
            raise RuntimeError("Dispatcher TLS proxy returned no local trust certificate")
        yield LocalDispatcherEndpoint(address=address, root_certificates=root_certificates)
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        await stderr_task
