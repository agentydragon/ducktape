"""Thin sandbox bridge between a WebSocket and a local agent CLI.

Which CLI is a backend (<backend.py>): this module launches the one it was told to launch and
pumps its stdio, and every decision that differs between CLIs sits behind that seam.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_delay, wait_exponential
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from haku.runtime.x.bridge.backend import BRIDGE_CREDENTIAL_VARIABLE, CliBackend
from haku.runtime.x.bridge.options import ClaudeBackend, claude_backend
from haku.runtime.x.bridge.protocol import (
    CONSOLE_TO_RUNNER,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Hello,
    SetupOutput,
    TextWebSocket,
    decode_object,
    encode_object,
)

logger = logging.getLogger(__name__)

# Where one line of bootstrap output goes. A callable rather than the websocket itself, because a
# frame has to pass through `OutboundLog` on its way out to be numbered, and the bootstrap has no
# business knowing that — nor should it be the one place on this side that sends an unnumbered
# frame, which is what a hole in the console's sequence would look like.
SetupNarration = Callable[[SetupOutput], Awaitable[None]]


@dataclass(frozen=True)
class Outbound:
    """One frame on its way to the console, and whether a later console may need it again.

    `replayable` is decided here, where the payload is already parsed, rather than by re-reading
    the JSON at send time. *Which* frames those are is the backend's to say (`CliBackend`): the
    class that cannot survive being sent twice is a fact about one CLI's protocol.

    The frame is carried as a model rather than serialized text because it is not finished yet:
    `OutboundLog.stamp` numbers it as it goes out, and that number is the log's ordering at the
    other end (<../../../plans/chat_runtime_projection.md> § 2b).
    """

    frame: ClaudeMessage | SetupOutput
    replayable: bool


# What the CLI may say before its pipes fill and it pauses for want of a listener. Sized for a
# whole turn rather than the gap in one: the runner forwards every frame including the streaming
# deltas, so a long answer written while nobody is connected runs to thousands. Still a buffer
# rather than a store — what makes a reconnect lossless is the resume cursor, not this number.
OUTBOUND_BUFFER = 10_000

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 20.0
# A sandbox held for a console that never returns is a wedged sandbox, which is worse than the
# wedged room it was protecting. Generous against a roll, decisive against an outage.
MAX_DISCONNECTED_SECONDS = 900.0

# How many already-sent frames are kept to hand a console that adopts this session. Bounded
# because this is a window over what a dying console may not have recorded, not a second copy of
# the rollout — the console's own log is that. Generous enough to cover a turn's worth of
# assistant messages and tool results, which is what a roll mid-turn can strand.
REPLAY_WINDOW = 500


class OutboundLog:
    """The runner's numbering of what it has sent, and the window it can still re-send.

    **The number is this end's to mint**, because this end is the one that survives: the console is
    replaced on every roll, while this process holds the CLI across as many sockets as that takes.
    A cursor a reconnecting console hands back has to name a number its peer assigned, or the peer
    cannot answer it — which is what makes catch-up a filter over this deque instead of a
    reconciliation against the console's database (<../../../plans/chat_runtime_projection.md> § 2b).

    Dense and monotonic over **everything** sent, replayable or not, so a hole the console sees is
    evidence of loss rather than of a class this end declined to count. Which frames are *retained*
    is the separate question `Outbound.replayable` answers.
    """

    def __init__(self, window: int = REPLAY_WINDOW):
        self._next_seq = 1
        self._retained: deque[tuple[int, str]] = deque(maxlen=window)

    def seed(self, resume_from: int | None) -> None:
        """Lift the counter above what the console already holds, if it holds anything.

        `max` rather than assignment: a cursor is a floor, so a runner whose own counter has already
        passed it keeps going from where it was, and one that restarted is lifted back above what
        the console holds instead of colliding with it from 1.

        Separate from `missed` and called earlier, because bootstrap narration goes out before a
        console is served and must not be numbered below what that console already recorded.
        """
        if resume_from is not None:
            self._next_seq = max(self._next_seq, resume_from + 1)

    def missed(self, resume_from: int | None) -> list[str]:
        """What a console holding *resume_from* has not been given, from the window still here.

        None is a console that does not number — one predating this, or one with nothing recorded —
        and gets the whole window, which is what every adoption used to get.
        """
        if resume_from is None:
            return [text for _, text in self._retained]
        return [text for seq, text in self._retained if seq > resume_from]

    def stamp(self, outbound: Outbound) -> str:
        """Number one frame and serialize it, retaining it if a later console may want it again.

        Numbered at send rather than at build, so the buffer's order is the wire's order and a
        retained frame re-offered later carries the number it first went out under — two consoles
        therefore agree on the integer naming one frame.
        """
        seq, self._next_seq = self._next_seq, self._next_seq + 1
        text = outbound.frame.model_copy(update={"seq": seq}).model_dump_json()
        if outbound.replayable:
            self._retained.append((seq, text))
        return text


class ClientWebSocketAdapter(TextWebSocket):
    """Adapt websockets' client connection to the transport's text-only surface."""

    def __init__(self, connection: ClientConnection):
        self._connection = connection

    async def send_text(self, data: str) -> None:
        await self._connection.send(data)

    async def receive_text(self) -> str:
        data = await self._connection.recv()
        if not isinstance(data, str):
            raise ValueError("the bridge requires text WebSocket frames")
        return data

    async def close(self) -> None:
        await self._connection.close()


# One entry, and that is the point rather than an oversight: the runner is the end that has to be
# told which CLI it is hosting. A second CLI ships as its own image and SandboxTemplate, which
# would set `HAKU_CLI_BACKEND` and change nothing else here.
BACKENDS: Final[Mapping[str, Callable[[Path | None], CliBackend]]] = {ClaudeBackend.name: claude_backend}


async def _queue_cli_line(outbound: MemoryObjectSendStream[Outbound], backend: CliBackend, line: bytes) -> None:
    """Wrap one CLI stream-JSON line in a `claude` envelope, skipping anything that is not one."""
    if not (stripped := line.strip()).startswith(b"{"):
        return
    payload = decode_object(stripped.decode())
    await outbound.send(Outbound(frame=ClaudeMessage(payload=payload), replayable=backend.replayable(payload)))


async def _forward_cli_frames(
    outbound: MemoryObjectSendStream[Outbound], backend: CliBackend, stdout: anyio.abc.ByteReceiveStream
) -> None:
    pending = b""
    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            await _queue_cli_line(outbound, backend, line)

    await _queue_cli_line(outbound, backend, pending)


async def _send_websocket_input(websocket: TextWebSocket, stdin: anyio.abc.ByteSendStream) -> None:
    while True:
        match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
            case EndInput():
                await stdin.aclose()
                return
            case ClaudeMessage(payload=payload):
                await stdin.send((encode_object(payload) + "\n").encode())
            case ClaudeLaunch():
                # Not a direction error — `start` is the console's to send — but a sequencing
                # one: it opens a connection, so a second one mid-conversation means the console
                # thinks it is talking to a runner that has not launched. The types cannot say
                # that, so this check stays where the two above went.
                raise ValueError("console sent a second launch frame mid-conversation")


async def _forward_cli_errors(outbound: MemoryObjectSendStream[Outbound], stderr: anyio.abc.ByteReceiveStream) -> None:
    """Forward what the CLI wrote to stderr, to this log and to the console.

    It used to go to `DEVNULL`, which is the one place a failure to start is explained: the
    console sees the exit status and nothing else, so `Claude Code exited with status 1` was the
    whole account of a rejected credential or a bad flag.

    Sent as `SetupOutput` because that frame is already "bytes the sandbox wrote" and adding a
    kind of its own would be a `PROTOCOL_VERSION` bump. The two ends do negotiate now — the
    runner's `Hello` carries its range and both take the `max` of the common one — but
    `SUPPORTED_VERSIONS` is still a single element, so a bump refuses a peer on the old version
    rather than degrading to it, and it would still break every session in flight on release.
    The console narrates and records it like any other sandbox output; telling the two apart is
    worth a frame kind once the supported range is wide enough to make one affordable.
    """
    async for chunk in stderr:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        # Not retained: narration is recorded as a frame with no identity, so a console cannot
        # tell a replayed line from a repeated one and would show the bootstrap twice.
        await outbound.send(Outbound(frame=SetupOutput(data=chunk), replayable=False))


async def _start_cli(backend: CliBackend, launch: ClaudeLaunch) -> anyio.abc.Process:
    resolved = backend.resolve(launch)
    return await anyio.open_process(
        resolved.command,
        cwd=resolved.cwd,
        env=resolved.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def _shutdown(process: anyio.abc.Process) -> int | None:
    """Stop the CLI, reporting the status it chose for itself — or None if we chose for it.

    The distinction is the difference between a failure and a teardown: a process we signalled
    reports the signal, so treating that as an exit status files every clean shutdown as
    `claude exited with status -15`.
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


async def _drain_cli(
    process: anyio.abc.Process,
    backend: CliBackend,
    outbound: MemoryObjectSendStream[Outbound],
    scope: anyio.CancelScope,
) -> None:
    """Read the CLI for as long as it lives, whether or not a console is listening.

    Long-lived rather than per-connection, which is what lets the process outlive a socket: its
    pipes keep draining into `outbound`, so a disconnected console costs the sandbox a pause
    rather than a wedge, and nothing it said is thrown away because there was nowhere to put it.
    When the buffer fills the reads stop, the pipes fill behind them, and the CLI waits — the
    honest behaviour for an agent nobody is listening to.

    Cancels *scope* when stdout ends, because that is the CLI exiting and there is then nothing
    left to serve any console with.
    """
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None
    assert stderr is not None
    async with anyio.create_task_group() as readers:
        # stderr ending says nothing about the conversation; stdout ending is the CLI's exit.
        readers.start_soon(_forward_cli_errors, outbound, stderr)
        await _forward_cli_frames(outbound, backend, stdout)
        readers.cancel_scope.cancel()
    scope.cancel()


async def _serve_console(
    websocket: TextWebSocket,
    process: anyio.abc.Process,
    outbound: MemoryObjectReceiveStream[Outbound],
    log: OutboundLog,
    resume_from: int | None = None,
) -> None:
    """Copy frames both ways for one console connection, returning when that connection ends.

    **The replay window is what makes a lost socket lossless.** A frame taken from the buffer and
    handed to a dying socket used to be gone: the console may have recorded it, or the send may
    have died in flight, and nothing could tell the two apart. So every retained frame is offered
    again to whichever console adopts the session next, and the console drops the ones it already
    has by their agent-assigned identity (<../../../cli_protocol/frame_identity.py>).

    That makes the exactness of the window an optimisation rather than a correctness argument —
    re-sending a frame the console already holds costs one `ON CONFLICT DO NOTHING`. What it must
    not do is *omit* one, which is why frames are retained as they are sent rather than as they
    are acknowledged: there is no acknowledgement, and inventing one would be a second protocol
    for what the console's own log already answers.

    *resume_from* is the console's own cursor, off its `start` frame, and it narrows that offer to
    what the console has not recorded — the exactness above turning from optimisation into
    something the console can also check, since the numbering is dense.
    """
    stdin = process.stdin
    assert stdin is not None
    async with anyio.create_task_group() as tasks:

        async def console_to_cli() -> None:
            try:
                await _send_websocket_input(websocket, stdin)
            except (EOFError, anyio.EndOfStream, ConnectionClosed):
                pass
            finally:
                tasks.cancel_scope.cancel()

        async def cli_to_console() -> None:
            try:
                log.seed(resume_from)
                if missed := log.missed(resume_from):
                    logger.info("Re-sending %d frame(s) the previous console may not have", len(missed))
                    for retained in missed:
                        await websocket.send_text(retained)
                async for frame in outbound:
                    await websocket.send_text(log.stamp(frame))
            except (ConnectionClosed, anyio.BrokenResourceError):
                pass
            finally:
                tasks.cancel_scope.cancel()

        tasks.start_soon(console_to_cli)
        tasks.start_soon(cli_to_console)


async def bridge_websocket_to_cli(websocket: TextWebSocket, *, backend: CliBackend, launch: ClaudeLaunch) -> None:
    """Run one CLI and serve exactly one console connection with it.

    The single-connection shape, kept because it is the whole of what a session without
    reconnection needs; `run` composes the same pieces with a process that outlives the socket.
    """
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](OUTBOUND_BUFFER)
    process = await _start_cli(backend, launch)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_drain_cli, process, backend, outbound_sender, tasks.cancel_scope)
            # No window, since there is no second connection to hand one to — but frames are still
            # numbered, because the console's log takes its ordering from that number either way.
            await _serve_console(websocket, process, outbound_receiver, OutboundLog(window=0), launch.resume_from)
            tasks.cancel_scope.cancel()
    finally:
        exited_with = await _shutdown(process)
        await websocket.close()
    if exited_with not in (0, None):
        raise RuntimeError(f"{backend.name} exited with status {exited_with}")


async def prepare_workspace(setup_path: Path, *, cwd: str, narrate: SetupNarration | None = None) -> None:
    """Run the shared sandbox bootstrap: git credentials and Haku's own checkouts.

    The same script the haku-sandbox exec target runs — see
    <../../../../cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh> — so this box gets
    the same `.netrc` and the same haku-state working copy rather than a second
    implementation that drifts from it.

    Run here, in the runner, rather than as an image entrypoint wrapper, so that *narrate*
    exists to report it: a clone is the longest thing between "provisioning" and an answer,
    and the console cannot report a step it cannot see.

    Its output is forwarded verbatim, in whatever chunks it arrives in, and written unchanged
    to this process's own stdout so the pod log keeps the same record the room gets. No
    decoding, no line-splitting, no filtering here — see `SetupOutput` for why that is the
    console's job.

    **Fatal on failure.** Without the checkout the session has no manual, and a Claude Code
    that starts anyway is the generic-assistant failure the system prompt exists to prevent —
    silent, and indistinguishable from Haku having a bad day.
    """
    process = await anyio.open_process(
        [str(setup_path)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert process.stdout is not None
    async for chunk in process.stdout:
        # `sys.stdout.buffer`, not `print`: the local log gets the same bytes the console does,
        # rather than a decoded-and-maybe-replaced rendering of them.
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if narrate is not None:
            await narrate(SetupOutput(data=chunk))
    if (status := await process.wait()) != 0:
        raise RuntimeError(f"workspace setup {setup_path} exited with status {status}")


def _narrator(websocket: TextWebSocket, log: OutboundLog) -> SetupNarration:
    """Send bootstrap output down *websocket*, numbered by *log* like everything else this end sends."""

    async def narrate(frame: SetupOutput) -> None:
        await websocket.send_text(log.stamp(Outbound(frame=frame, replayable=False)))

    return narrate


async def _receive_launch(websocket: TextWebSocket) -> ClaudeLaunch:
    """Say which versions this image speaks, then read the launch the console chose.

    The hello goes first on **every** connection, not only the first: the console that adopts a
    session after a roll is a different process from the one that started it, and it has to be
    told the same thing. It is also cheap to a console too old to expect it — that one never reads
    before it writes, so the frame simply goes unread.
    """
    await websocket.send_text(Hello().model_dump_json())
    if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), ClaudeLaunch):
        raise ValueError(f"first bridge frame must be a launch, got {type(launch).__name__}")
    return launch


def _worth_redialling(error: BaseException) -> bool:
    """Whether a failed dial is a console that is not there *yet*, rather than one refusing us.

    See `NOT_ADMITTED_CODE` for why a refusal arrives as a 4xx handshake response instead of a
    close code. A 5xx is the Gateway with no ready backend — which is exactly what a console roll
    looks like from in here — and an `OSError` is the connection itself failing; both are worth
    waiting out, and neither used to be: `InvalidStatus` is not an `OSError`, so a single 503
    mid-roll escaped the loop and took the sandbox with it.

    **The 5xx arm is load-bearing beyond the Gateway, so do not tighten it to a status list.** A
    console that is up but whose session is still leased by a replica shutting down answers 503
    deliberately, through the ASGI denial-response extension, precisely so this returns True — see
    `BridgeAuthentication.HELD`. Narrowing this to "only the Gateway says 5xx" would restore the
    bug that made every console roll cost a session.
    """
    if isinstance(error, InvalidStatus):
        return error.response.status_code >= 500
    return isinstance(error, OSError | InvalidHandshake)


async def _dial(websocket_url: str, headers: dict[str, str] | None) -> ClientConnection:
    """Connect, waiting out a console that is missing for as long as that is worth doing.

    The clock starts at each call, so the budget is "how long since this runner last had a
    console" rather than how long the session has run — a sandbox is worth holding through any
    number of rolls, and worth releasing after one outage that does not end.
    """

    async def dial_once() -> ClientConnection:
        return await connect(websocket_url, additional_headers=headers)

    return await AsyncRetrying(
        retry=retry_if_exception(_worth_redialling),
        wait=wait_exponential(multiplier=RECONNECT_BASE_DELAY, max=RECONNECT_MAX_DELAY),
        stop=stop_after_delay(MAX_DISCONNECTED_SECONDS),
        before_sleep=before_sleep_log(logger, logging.INFO),
        reraise=True,
    )(dial_once)


async def run(
    websocket_url: str, backend: CliBackend, bearer_token: str | None, setup_path: Path | None = None
) -> None:
    """Serve one CLI to whichever console is up, across as many connections as that takes.

    **The CLI outlives the connection.** It used to die with it — one socket, one process, and
    `bridge_websocket_to_cli` terminating the CLI in its `finally` — so a console roll ended
    the conversation, and the reconnect Kubernetes then forced was refused by a console that
    admitted a session only once. Six rolls a day made that the normal end of a session.

    So this dials, serves, and dials again, holding the process across the gap. What the console
    sends on a later connection is a `start` frame it built fresh, and everything describing the
    process is deliberately **ignored**: the launch decided the argv, the system prompt and the MCP
    wiring of a process that is already running, and none of those can be re-applied to it. The
    runner is the honest owner of "the handshake already happened", because it is the end that owns
    the process.

    The one field that is *not* ignored is `resume_from`, which describes the **console** rather
    than the process — how much of this session's log it holds — and is what turns the replay
    window from "everything, deduplicated later" into "exactly what you are missing".

    Giving up is bounded. A sandbox held for a console that never returns trades a wedged room
    for a wedged sandbox, so after `MAX_DISCONNECTED_SECONDS` without one this exits and lets the
    claim be reclaimed.
    """
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](OUTBOUND_BUFFER)
    process: anyio.abc.Process | None = None
    # Retained across connections, which is the whole point: it is what a console that adopts this
    # session mid-turn is handed before it starts hearing live frames. The numbering is retained
    # with it, so one session's frames are one sequence however many consoles serve it.
    log = OutboundLog()

    try:
        async with anyio.create_task_group() as session:
            while True:
                try:
                    connection = await _dial(websocket_url, headers)
                except (OSError, InvalidHandshake) as error:
                    # Either the console refused this runner or it never came back inside
                    # `MAX_DISCONNECTED_SECONDS`; the error text says which. Both end the sandbox,
                    # because one held for a console that never returns is worse than the wedged
                    # room it was protecting.
                    logger.info("Giving up on the console (%s); releasing this sandbox", error)
                    break
                try:
                    websocket = ClientWebSocketAdapter(connection)
                    launch = await _receive_launch(websocket)
                    # Before the bootstrap, not with the replay: narration is numbered too, and a
                    # console that already holds frames must not be sent one below its cursor.
                    log.seed(launch.resume_from)
                    if process is None:
                        if setup_path is not None:
                            await prepare_workspace(setup_path, cwd=launch.cwd, narrate=_narrator(websocket, log))
                        process = await _start_cli(backend, launch)
                        # Long-lived, so nothing the CLI writes is lost to a closed socket and its
                        # pipes never fill: they drain into the buffer either way, and the buffer's
                        # backpressure pauses the CLI rather than dropping what it said.
                        session.start_soon(_drain_cli, process, backend, outbound_sender, session.cancel_scope)
                    await _serve_console(websocket, process, outbound_receiver, log, launch.resume_from)
                except ConnectionClosed:
                    # This connection ending says nothing about the session; `_dial` decides
                    # whether there is still a console worth waiting for.
                    pass
                finally:
                    await connection.close()
                # Not a backoff — `_dial` owns that — but a floor, so a console that admits this
                # runner and then immediately hangs up costs one redial a second rather than as
                # many as this loop can turn.
                await anyio.sleep(RECONNECT_BASE_DELAY)
            # Ends `_drain_cli`, which otherwise holds this group open for as long as the CLI
            # lives — so giving up on the console would hang instead of releasing the sandbox.
            session.cancel_scope.cancel()
    finally:
        if process is not None and (exited_with := await _shutdown(process)) not in (0, None):
            raise RuntimeError(f"{backend.name} exited with status {exited_with}")


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge a Haku Console WebSocket to an agent CLI's stdio.")
    parser.add_argument("--websocket-url", default=os.environ.get("HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"))
    parser.add_argument("--session-id", default=os.environ.get("HAKU_CLAUDE_SESSION_ID"))
    parser.add_argument(
        "--backend", choices=sorted(BACKENDS), default=os.environ.get("HAKU_CLI_BACKEND", ClaudeBackend.name)
    )
    # Unset leaves the executable to the backend, which reads the variable its own image sets
    # (`options.EXECUTABLE_VARIABLE` for Claude); this is for a local run against a CLI elsewhere.
    parser.add_argument("--cli-path", type=Path)
    # Unset means "no bootstrap", which is what the transport's own tests and any bare
    # local run want; the image sets it. Named for Claude historically only — the bootstrap it
    # points at checks haku-state out and knows nothing about which CLI follows it.
    parser.add_argument("--setup-path", type=Path, default=_optional_path(os.environ.get("HAKU_CLAUDE_SETUP")))
    args = parser.parse_args()
    if not args.websocket_url:
        parser.error("--websocket-url or HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL is required")
    if args.session_id:
        args.websocket_url = f"{args.websocket_url.rstrip('/')}/{args.session_id}"
    return args


def main() -> None:
    args = parse_args()
    anyio.run(
        run,
        args.websocket_url,
        BACKENDS[args.backend](args.cli_path),
        os.environ.get(BRIDGE_CREDENTIAL_VARIABLE),
        args.setup_path,
    )


if __name__ == "__main__":
    main()
