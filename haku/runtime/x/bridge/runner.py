"""Thin sandbox bridge between a WebSocket and a local agent CLI.

Which CLI it is stays behind the backend seam (<backend.py>); this module only launches what it
was told to and pumps its stdio.
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

# Where one line of bootstrap output goes. A callable rather than the websocket, so the frame still
# passes through `OutboundLog` to be numbered: an unnumbered frame is a hole in the console's
# sequence.
SetupNarration = Callable[[SetupOutput], Awaitable[None]]


@dataclass(frozen=True)
class Outbound:
    """One frame on its way to the console, and whether a later console may need it again.

    Which frames are replayable is the backend's to say (`CliBackend`): the class that cannot
    survive being sent twice is a fact about one CLI's protocol.

    Carried as a model rather than serialized text because `OutboundLog.stamp` still has to number
    it, and that number is what the other end's resume cursor is built from.
    """

    frame: ClaudeMessage | SetupOutput
    replayable: bool


# What the CLI may say before its pipes fill and it pauses for want of a listener. Sized for a whole
# turn, since every streaming delta is forwarded and a long answer runs to thousands. Still a buffer
# rather than a store — what makes a reconnect lossless is the resume cursor, not this number.
OUTBOUND_BUFFER = 10_000

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 20.0
# A sandbox held for a console that never returns is worse than the wedged room it was protecting.
MAX_DISCONNECTED_SECONDS = 900.0

# How many already-sent frames are kept to hand a console that adopts this session: a window over
# what a dying console may not have recorded, not a second copy of the rollout. Sized for a turn's
# assistant messages and tool results, which is what a roll mid-turn can strand.
REPLAY_WINDOW = 500


class OutboundLog:
    """The runner's numbering of what it has sent, and the window it can still re-send.

    **The number is this end's to mint**, because this end survives: the console is replaced on
    every roll while this process holds the CLI across as many sockets as that takes. A cursor a
    reconnecting console hands back has to name a number its peer assigned, which makes catch-up a
    filter over this deque rather than a reconciliation against the console's database.

    Dense and monotonic over **everything** sent, replayable or not, so a hole the console sees is
    evidence of loss rather than of a class this end declined to count. Which frames are *retained*
    is the separate question `Outbound.replayable` answers.
    """

    def __init__(self, window: int = REPLAY_WINDOW):
        self._next_seq = 1
        self._retained: deque[tuple[int, str]] = deque(maxlen=window)

    def seed(self, resume_from: int | None) -> None:
        """Lift the counter above what the console already holds, if it holds anything.

        `max` rather than assignment: a cursor is a floor, so a runner whose counter is already past
        it keeps going, and a restarted one is lifted clear instead of colliding from 1.

        Called earlier than `missed`, because bootstrap narration goes out before a console is
        served and must not be numbered below what that console already recorded.
        """
        if resume_from is not None:
            self._next_seq = max(self._next_seq, resume_from + 1)

    def missed(self, resume_from: int | None) -> list[str]:
        """What a console holding *resume_from* has not been given, from the window still here.

        None is a console that does not number, or one with nothing recorded; it gets the whole
        window.
        """
        if resume_from is None:
            return [text for _, text in self._retained]
        return [text for seq, text in self._retained if seq > resume_from]

    def stamp(self, outbound: Outbound) -> str:
        """Number one frame and serialize it, retaining it if a later console may want it again.

        Numbered at send rather than at build, so the buffer's order is the wire's order and a
        replayed frame keeps the number it first went out under — two consoles therefore agree on
        the integer naming one frame.
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


# One entry by design: a second CLI ships as its own image and SandboxTemplate, setting
# `HAKU_CLI_BACKEND` and changing nothing else here.
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
                # A sequencing error the types cannot express: `start` opens a connection, so a
                # second one mid-conversation means the console thinks this runner never launched.
                raise ValueError("console sent a second launch frame mid-conversation")


async def _forward_cli_errors(outbound: MemoryObjectSendStream[Outbound], stderr: anyio.abc.ByteReceiveStream) -> None:
    """Forward what the CLI wrote to stderr, to this log and to the console.

    stderr is the one place a failure to start is explained; without it the console sees only
    `Claude Code exited with status 1` for a rejected credential or a bad flag.

    Sent as `SetupOutput`, which is already "bytes the sandbox wrote", because a kind of its own
    would be a `PROTOCOL_VERSION` bump — and `SUPPORTED_VERSIONS` holds one element, so a bump
    refuses peers on the old version rather than degrading to it. Worth a frame kind of its own once
    the supported range is wide enough to afford one.
    """
    async for chunk in stderr:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        # Not retained: narration has no frame identity, so a console cannot tell a replayed line
        # from a repeated one and would show the bootstrap twice.
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


async def _drain_cli(
    process: anyio.abc.Process,
    backend: CliBackend,
    outbound: MemoryObjectSendStream[Outbound],
    scope: anyio.CancelScope,
) -> None:
    """Read the CLI for as long as it lives, whether or not a console is listening.

    Long-lived rather than per-connection, which is what lets the process outlive a socket: the
    pipes keep draining into `outbound`, and when that buffer fills the reads stop and the CLI
    waits rather than losing what it said.

    Cancels *scope* when stdout ends, since that is the CLI exiting and there is then nothing left
    to serve any console with.
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

    **The replay window is what makes a lost socket lossless.** Nothing here can tell whether a
    frame handed to a dying socket was recorded, so every retained frame is offered again to
    whichever console adopts the session next, and the console drops duplicates by their
    agent-assigned identity (<../../../cli_protocol/frame_identity.py>).

    Re-sending a frame the console already holds costs one `ON CONFLICT DO NOTHING`; *omitting* one
    is the real failure, which is why frames are retained as they are sent rather than as they are
    acknowledged — there is no acknowledgement.

    *resume_from* is the console's own cursor, off its `start` frame, narrowing the offer to what it
    has not recorded.
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

    `run` composes the same pieces with a process that outlives the socket.
    """
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](OUTBOUND_BUFFER)
    process = await _start_cli(backend, launch)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_drain_cli, process, backend, outbound_sender, tasks.cancel_scope)
            # No window, since there is no second connection to hand one to; still numbered, because
            # the console's log takes its ordering from that number either way.
            await _serve_console(websocket, process, outbound_receiver, OutboundLog(window=0), launch.resume_from)
            tasks.cancel_scope.cancel()
    finally:
        exited_with = await _shutdown(process)
        await websocket.close()
    if exited_with not in (0, None):
        raise RuntimeError(f"{backend.name} exited with status {exited_with}")


async def prepare_workspace(setup_path: Path, *, cwd: str, narrate: SetupNarration | None = None) -> None:
    """Run the shared sandbox bootstrap: git credentials and Haku's own checkouts.

    The same script the haku-sandbox exec target runs
    (<../../../../cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh>), so this box gets the
    same `.netrc` and haku-state working copy.

    Run in the runner rather than as an image entrypoint wrapper so *narrate* can report it: a clone
    is the longest thing between "provisioning" and an answer.

    Output is forwarded verbatim, in whatever chunks it arrives in, and written unchanged to this
    process's stdout so the pod log keeps the record the room gets. Decoding and line-splitting are
    the console's job — see `SetupOutput`.

    **Fatal on failure.** Without the checkout the session has no manual, and a Claude Code that
    starts anyway is silently a generic assistant.
    """
    process = await anyio.open_process(
        [str(setup_path)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert process.stdout is not None
    async for chunk in process.stdout:
        # `sys.stdout.buffer`, not `print`: the local log gets the same bytes the console does,
        # not a decoded-and-maybe-replaced rendering of them.
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

    The hello goes first on **every** connection, not only the first: a console adopting a session
    after a roll is a different process and has to be told the same thing. A console too old to
    expect it never reads before it writes, so the frame simply goes unread.
    """
    await websocket.send_text(Hello().model_dump_json())
    if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), ClaudeLaunch):
        raise ValueError(f"first bridge frame must be a launch, got {type(launch).__name__}")
    return launch


def _worth_redialling(error: BaseException) -> bool:
    """Whether a failed dial is a console that is not there *yet*, rather than one refusing us.

    See `NOT_ADMITTED_CODE` for why a refusal arrives as a 4xx handshake response instead of a close
    code. A 5xx is the Gateway with no ready backend — a console roll, from in here — and an
    `OSError` is the connection itself failing. Separate arms because `InvalidStatus` is not an
    `OSError`, so a 503 mid-roll would otherwise escape the loop and take the sandbox with it.

    **Do not tighten the 5xx arm to a status list.** A console whose session is still leased by a
    replica shutting down answers 503 deliberately, through the ASGI denial-response extension,
    precisely so this returns True — see `BridgeAuthentication.HELD`.
    """
    if isinstance(error, InvalidStatus):
        return error.response.status_code >= 500
    return isinstance(error, OSError | InvalidHandshake)


async def _dial(websocket_url: str, headers: dict[str, str] | None) -> ClientConnection:
    """Connect, waiting out a console that is missing for as long as that is worth doing.

    The clock starts at each call, so the budget is "how long since this runner last had a console"
    rather than how long the session has run: any number of rolls is survivable, one unending outage
    is not.
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

    **The CLI outlives the connection**, which is what keeps a console roll from ending the
    conversation. A later connection brings a freshly built `start` frame whose process fields are
    **ignored**: argv, system prompt and MCP wiring belong to a process already running and cannot
    be re-applied to it.

    The exception is `resume_from`, which describes the **console** — how much of this session's log
    it holds — and narrows the replay window to exactly what it is missing.

    After `MAX_DISCONNECTED_SECONDS` with no console this exits and lets the claim be reclaimed.
    """
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](OUTBOUND_BUFFER)
    process: anyio.abc.Process | None = None
    # Retained across connections: it is what a console adopting this session mid-turn is handed
    # before it hears live frames, and it keeps one session's frames on one sequence however many
    # consoles serve it.
    log = OutboundLog()

    try:
        async with anyio.create_task_group() as session:
            while True:
                try:
                    connection = await _dial(websocket_url, headers)
                except (OSError, InvalidHandshake) as error:
                    # The console refused this runner, or never came back inside
                    # `MAX_DISCONNECTED_SECONDS`; the error text says which. Both end the sandbox.
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
                        # Long-lived, so nothing the CLI writes is lost to a closed socket: the
                        # pipes drain into the buffer either way, whose backpressure pauses the CLI
                        # rather than dropping what it said.
                        session.start_soon(_drain_cli, process, backend, outbound_sender, session.cancel_scope)
                    await _serve_console(websocket, process, outbound_receiver, log, launch.resume_from)
                except ConnectionClosed:
                    # This connection ending says nothing about the session; `_dial` decides
                    # whether there is still a console worth waiting for.
                    pass
                finally:
                    await connection.close()
                # Not a backoff — `_dial` owns that — but a floor, so a console that admits this
                # runner and immediately hangs up costs one redial a second, not a spin.
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
    # Unset means "no bootstrap", which is what tests and a bare local run want; the image sets it.
    # The variable's name says Claude, but the bootstrap only checks haku-state out and knows
    # nothing about which CLI follows it.
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
