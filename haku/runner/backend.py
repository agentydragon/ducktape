"""The per-harness seam, and the stdio machinery a subprocess harness runs on.

Each harness owns its whole run-loop behind `Harness.run(launch, session)`: it starts its binary,
speaks its native protocol end to end — handshake and all — and emits neutral operations through
the <session_api.py> `SessionApi` the runner hands it. The runner (<runner.py>) owns only the
harness-invariant lifecycle around that seam; it never inspects a native payload or drives a turn.

Both harnesses this bridge ships happen to speak newline-delimited JSON over subprocess stdio, so
the launch primitives (`ProcessLaunch`, `child_environment`) and the stdio pump (`start_process`,
`read_json_frames`, `forward_stderr`, `shutdown`, `StdinWriter`) live here for both to share. A
harness that spoke something else would implement `run` without them; that is the point of putting
the whole loop behind the seam rather than a fixed set of hooks the runner calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import anyio

from haku.runner.protocol import HarnessLaunch, decode_object, encode_object
from haku.runner.session_api import SessionApi

# The exact-session credential used by the runner bridge and by the Agent at Console MCP. The
# runner keeps the claim-owned value out of launch overlays, and the console's deploy config
# refuses the name as a provider API-key variable.
BRIDGE_CREDENTIAL_VARIABLE = "HAKU_RUNNER_TOKEN"
_PROXY_ENVIRONMENT_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@dataclass(frozen=True, slots=True)
class ProcessLaunch:
    """One CLI process, fully decided: which binary, which argv, where, and in what environment.

    The console's `HarnessLaunch` carries every part of this except the binary, because the
    binary is the one part the console cannot know — it is a path inside a sandbox image whose
    tag the SandboxTemplate chose. Resolving the two into this is the harness's whole job.
    """

    executable: Path
    arguments: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]

    @property
    def command(self) -> list[str]:
        return [str(self.executable), *self.arguments]


def child_environment(launch: HarnessLaunch) -> dict[str, str]:
    """Overlay launch values while retaining the claim-owned exact-session credential.

    The Console sends proxy topology in the launch, while the claim-owned bridge bearer remains
    in the runner Pod environment. URL userinfo is used only in the child process environment so
    ordinary HTTP clients send ``Proxy-Authorization`` without a second secret or a launch-frame
    credential.
    """
    environment = {
        **os.environ,
        **{key: value for key, value in launch.environment.items() if key != BRIDGE_CREDENTIAL_VARIABLE},
    }
    if proxy_variables := set(launch.environment) & set(_PROXY_ENVIRONMENT_VARIABLES):
        bearer = os.environ.get(BRIDGE_CREDENTIAL_VARIABLE)
        if not bearer:
            raise RuntimeError("proxy environment requires a bridge bearer")
        for variable in proxy_variables:
            environment[variable] = _proxy_url_with_bearer(environment[variable], bearer)
    return environment


def _proxy_url_with_bearer(proxy_url: str, bearer: str) -> str:
    """Put *bearer* in proxy URL userinfo, rejecting deploy URLs with another credential."""
    parsed = urlsplit(proxy_url)
    try:
        hostname = parsed.hostname
    except ValueError as error:
        raise RuntimeError("proxy URL has an invalid host") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or hostname is None:
        raise RuntimeError("proxy URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("proxy URL must not already contain credentials")
    return urlunsplit(parsed._replace(netloc=f":{quote(bearer, safe='')}@{parsed.netloc}"))


class Harness(Protocol):
    """One agent CLI this bridge knows how to run, at the neutral-operation generation.

    The runner selects one by `--harness`, hands it the launch and the session toolkit, and starts
    it once. From there the harness owns everything native: starting its binary, its handshake,
    what a dispatched prompt and an interrupt are written as, and what its stream means as neutral
    operations. One `run` per session, across as many console connections as that session takes;
    it returns when its process exits, and raises to fail the session.
    """

    @property
    def name(self) -> str:
        """How this harness is named to an operator: `--harness`, and the exit-status error."""

    async def run(self, launch: HarnessLaunch, session: SessionApi) -> None:
        """Serve one session: start the binary, speak its protocol, emit neutral operations."""
        ...


class StdinWriter:
    """Line writes into the CLI, serialized: a harness's command loop and its stream loop both
    write native input, and interleaving two halves of two lines would hand the CLI garbage."""

    def __init__(self, stdin: anyio.abc.ByteSendStream):
        self._stdin = stdin
        self._lock = anyio.Lock()

    async def write_object(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._stdin.send((encode_object(payload) + "\n").encode())

    async def aclose(self) -> None:
        async with self._lock:
            await self._stdin.aclose()


async def start_process(resolved: ProcessLaunch) -> anyio.abc.Process:
    """Start one harness binary with its resolved argv, working directory and environment."""
    return await anyio.open_process(
        resolved.command,
        cwd=resolved.cwd,
        env=resolved.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def read_json_frames(stdout: anyio.abc.ByteReceiveStream) -> AsyncIterator[dict[str, Any]]:
    """Yield each newline-delimited JSON object the harness writes to stdout, until stdout ends.

    Non-JSON lines (a CLI that logs plain text before its protocol starts) are skipped, as the v3
    pump did: only a line that begins with `{` is a frame.
    """
    pending = b""

    def parse(line: bytes) -> dict[str, Any] | None:
        stripped = line.strip()
        return decode_object(stripped.decode()) if stripped.startswith(b"{") else None

    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if (frame := parse(line)) is not None:
                yield frame
    if (frame := parse(pending)) is not None:
        yield frame


async def forward_stderr(stderr: anyio.abc.ByteReceiveStream, session: SessionApi) -> None:
    """Forward what the CLI wrote to stderr, to this log and to the console.

    stderr is the one place a failure to start is explained; without it the console sees only the
    selected harness exiting with status 1 for a rejected credential or a bad flag. Sent as
    `SetupOutput`, which is already "bytes the sandbox wrote".
    """
    async for chunk in stderr:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        await session.stderr_output(chunk)


async def shutdown(process: anyio.abc.Process) -> int | None:
    """Stop the CLI, reporting the status it chose for itself — or None if we chose for it.

    A process we signalled reports the signal, so treating that as an exit status would file every
    clean shutdown as `claude exited with status -15`.
    """
    # A CLI that has already exited is reaped here, so its own status is the one reported.
    with anyio.move_on_after(1):
        await process.wait()
    exited_with = process.returncode

    if process.returncode is None:
        process.terminate()
    with anyio.move_on_after(5, shield=True):
        await process.wait()
    if process.returncode is None:
        process.kill()
        await process.wait()
    return exited_with
