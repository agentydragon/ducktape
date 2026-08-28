"""Sandbox bridge between a WebSocket and a local agent CLI, at the neutral-operation generation.

Which CLI it is stays behind the backend seam (<backend.py>); this module launches what it was
told to, pumps its stdio, and — since the #4667 generation cut — owns the native protocol's
meaning: every stdout frame is numbered once, recorded on the wire as an opaque `HarnessFrame`,
and folded by the backend's `HarnessDriver` into the neutral-operation journal
(<neutral_operations.py>) that the Console commits and ACKs. The Console writes no native input
any more: it dispatches prompts by durable id (`PromptDispatch`) and asks for interrupts
(`Interrupt`); the runner composes the native frames, injects them, and echoes each injection as a
numbered `injected` frame so the durable record keeps both directions.

**One sequence numbers everything this end sends** — stdout frames, setup narration, injected
input — minted where the event happens rather than where the socket is, so the seq the projector
stamps into provenance is the seq the recorded frame carries, and both survive the socket that
happens to be up.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_delay, wait_exponential
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from haku.runtime.x.bridge.backend import BRIDGE_CREDENTIAL_VARIABLE, CliBackend, HarnessDriver
from haku.runtime.x.bridge.backend_registry import BackendFactory, runner_backends
from haku.runtime.x.bridge.claude_projection import Projected
from haku.runtime.x.bridge.neutral_operations import (
    GENERATION,
    BatchAck,
    ConsoleResume,
    Operation,
    OperationBatch,
    RunnerHello,
    TurnAborted,
    TurnEnded,
    TurnOpened,
)
from haku.runtime.x.bridge.operation_journal import OperationJournal
from haku.runtime.x.bridge.protocol import (
    CONSOLE_TO_RUNNER,
    KUBERNETES_PROXY_URL_ENV,
    NOT_ADMITTED_CODE,
    RUNNER_SETUP_ENV,
    ConsoleJournal,
    EndInput,
    HarnessFrame,
    HarnessLaunch,
    Hello,
    Interrupt,
    PromptDispatch,
    RunnerJournal,
    SetupOutput,
    TextWebSocket,
    decode_object,
    encode_object,
)

logger = logging.getLogger(__name__)

# Where one line of bootstrap output goes. A callable rather than the websocket, so the frame still
# passes through the pump to be numbered: an unnumbered frame is a hole in the console's sequence.
SetupNarration = Callable[[SetupOutput], Awaitable[None]]

# What the CLI may say before its pipes fill and it pauses for want of a listener. Sized for a whole
# turn, since every streaming delta is forwarded and a long answer runs to thousands. Still a buffer
# rather than a store — what makes a reconnect lossless is the two resume cursors, not this number.
OUTBOUND_BUFFER = 10_000

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 20.0
# A sandbox held for a console that never returns is worse than the wedged room it was protecting.
MAX_DISCONNECTED_SECONDS = 900.0

# How many already-sent frames are kept to hand a console that adopts this session: a window over
# what a dying console may not have recorded, not a second copy of the rollout. Sized for a turn's
# assistant messages and tool results, which is what a roll mid-turn can strand. Journal batches
# are retained separately and unbounded, by the ACK contract (<operation_journal.py>).
REPLAY_WINDOW = 500


class ConsoleRefusedError(RuntimeError):
    """The console refused this runner for good — a generation mismatch, a consumed credential, a
    session already over. No redial can change it, so the sandbox exits and releases its claim."""


class StdinWriter:
    """Line writes into the CLI, serialized: the console handler and the control-refusal path both
    write, and interleaving two halves of two lines would hand the CLI garbage."""

    def __init__(self, stdin: anyio.abc.ByteSendStream):
        self._stdin = stdin
        self._lock = anyio.Lock()

    async def write_object(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._stdin.send((encode_object(payload) + "\n").encode())

    async def aclose(self) -> None:
        async with self._lock:
            await self._stdin.aclose()


def _journal_text(message: RunnerHello | OperationBatch) -> str:
    return RunnerJournal(message=message).model_dump_json()


class SessionPump:
    """One session's numbering, retention, projection and journaling, across every socket.

    **The number is this end's to mint**, because this end survives: the console is replaced on
    every roll while this process holds the CLI across as many sockets as that takes. Everything
    stamped goes out through one buffer under one lock, so the wire order is the stamp order; a
    reconnect replays retained frames above the console's frame cursor and retained journal
    batches above its batch cursor, and the console deduplicates both — frames by `runner_seq`,
    batches by idempotent commit.
    """

    def __init__(self, driver: HarnessDriver, outbound: MemoryObjectSendStream[str], *, window: int = REPLAY_WINDOW):
        self._driver = driver
        self._journal = OperationJournal()
        self._outbound = outbound
        self._lock = anyio.Lock()
        self._next_seq = 1
        self._retained: deque[tuple[int, str]] = deque(maxlen=window)
        # An interrupt was asked and no turn end has answered it yet. Cleared by the end it
        # rewrites, or by a turn opening — a fresh exchange means the abort's target already ended.
        self._abort_pending = False
        # Dispatch is idempotent by prompt id: the console re-dispatches unadmitted prompts after
        # a reconnect, and an id already taken is the same prompt, not a second one.
        self._taken_prompts: set[UUID] = set()

    def seed(self, resume_from: int | None) -> None:
        """Lift the counter above what the console already holds, if it holds anything.

        `max` rather than assignment: a cursor is a floor, so a counter already past it keeps
        going. Called before any narration, which is numbered too and must not land below what
        the console already recorded.
        """
        if resume_from is not None:
            self._next_seq = max(self._next_seq, resume_from + 1)

    def missed(self, resume_from: int | None) -> list[str]:
        """The retained frames a console holding *resume_from* has not been given.

        None is a console with nothing recorded; it gets the whole window. Journal replay is the
        journal's own (`resumed`); the two cursors are independent by design.
        """
        if resume_from is None:
            return [text for _, text in self._retained]
        return [text for seq, text in self._retained if seq > resume_from]

    def resumed(self, resume: ConsoleResume) -> list[str]:
        """Everything the journal owes a (re)connected console, from its durable batch cursor.

        Deliberately lock-free (as `missed`): both run between connections, where the one other
        stamper may be parked mid-`send` on a full buffer holding the lock — its already-stamped
        text is either inside the replay window (sent here, deduplicated later) or still in the
        buffer (sent once when the serve loop drains it), so nothing is lost or doubled durably.
        """
        return [_journal_text(batch) for batch in self._journal.resume(resume.acked_batch_seq)]

    async def narration(self, websocket: TextWebSocket, output: SetupOutput) -> None:
        """Number one bootstrap chunk and send it directly — the serve loop is not running yet."""
        async with self._lock:
            text, _ = self._stamp(output, retain=False)
            await websocket.send_text(text)

    async def stderr_output(self, chunk: bytes) -> None:
        """Number one CLI stderr chunk into the buffer; not retained, because the console renders
        chunks into lines and cannot identify a replayed chunk by position."""
        async with self._lock:
            text, _ = self._stamp(SetupOutput(data=chunk), retain=False)
            await self._outbound.send(text)

    async def initialized(self, stdin: StdinWriter) -> None:
        """Write the harness handshake, if this harness has one, echoing it into the record."""
        payload = self._driver.initialize()
        if payload is None:
            return
        await self._inject(payload)
        await stdin.write_object(payload)

    async def observed(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One CLI stdout frame: number it, send it, journal its meaning; returns the native reply
        to write back for a CLI-initiated control request, already echoed into the record."""
        async with self._lock:
            wire, seq = self._stamp(HarnessFrame(frame=payload), retain=True)
            await self._outbound.send(wire)
            for text in self._journalled(self._driver.observe(seq, payload)):
                await self._outbound.send(text)
            reply = self._driver.answer_control_request(payload)
            if reply is not None:
                echo, _ = self._stamp(HarnessFrame(frame=reply, injected=True), retain=True)
                await self._outbound.send(echo)
            return reply

    async def admit(self, dispatch: PromptDispatch) -> dict[str, Any] | None:
        """One dispatched prompt: the native frame to write to the CLI, or None for a duplicate.

        The admission is journalled at the injection fence with the journal's own frontier, and
        the injected frame is echoed under the seq the provenance names — all before the caller
        writes the CLI, so the record can never show output of a prompt it has no injection for.
        """
        if dispatch.prompt_id in self._taken_prompts:
            return None
        self._taken_prompts.add(dispatch.prompt_id)
        payload = self._driver.compose_prompt(dispatch.text)
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            projected = self._driver.admit(
                dispatch.prompt_id, after_batch_seq=self._journal.admission_frontier, frame_seq=seq
            )
            for text in self._journalled(projected):
                await self._outbound.send(text)
        return payload

    async def interrupt(self) -> dict[str, Any] | None:
        """The operator's stop: the native interrupt to write, or None for a harness without one.

        The next turn end this pump journals is rewritten `aborted` — the side that asked records
        the abort, and under this generation the runner is the side that asks the harness.
        """
        payload = self._driver.compose_interrupt()
        if payload is None:
            return None
        await self._inject(payload)
        self._abort_pending = True
        return payload

    async def flushed(self) -> None:
        """The diagnostics-only tail a CLI may end on, released through the journal's own gate."""
        async with self._lock:
            for batch in self._journal.flush():
                await self._outbound.send(_journal_text(batch))

    async def acked(self, ack: BatchAck) -> None:
        """The console's cumulative ACK: drop covered retention, send whatever it released."""
        async with self._lock:
            for batch in self._journal.acked(ack.acked_batch_seq):
                await self._outbound.send(_journal_text(batch))

    async def _inject(self, payload: dict[str, Any]) -> int:
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            return seq

    def _stamp(self, frame: HarnessFrame | SetupOutput, *, retain: bool) -> tuple[str, int]:
        seq, self._next_seq = self._next_seq, self._next_seq + 1
        text = frame.model_copy(update={"seq": seq}).model_dump_json()
        if retain:
            self._retained.append((seq, text))
        return text, seq

    def _journalled(self, projected: Projected) -> list[str]:
        batches = self._journal.record(self._abort_rewritten(projected.operations), projected.unprojected)
        return [_journal_text(batch) for batch in batches]

    def _abort_rewritten(self, operations: tuple[Operation, ...]) -> tuple[Operation, ...]:
        if not self._abort_pending:
            return operations
        rewritten: list[Operation] = []
        for operation in operations:
            match operation:
                case TurnOpened():
                    # A fresh exchange: whatever the interrupt was for has already ended.
                    self._abort_pending = False
                    rewritten.append(operation)
                case TurnEnded() if self._abort_pending:
                    self._abort_pending = False
                    rewritten.append(
                        TurnEnded(turn_id=operation.turn_id, end=TurnAborted(), provenance=operation.provenance)
                    )
                case _:
                    rewritten.append(operation)
        return tuple(rewritten)


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


async def _forward_cli_frames(pump: SessionPump, stdin: StdinWriter, stdout: anyio.abc.ByteReceiveStream) -> None:
    """Read the CLI's stdout for as long as it speaks, numbering and journalling every frame."""
    pending = b""

    async def take(line: bytes) -> None:
        if not (stripped := line.strip()).startswith(b"{"):
            return
        reply = await pump.observed(decode_object(stripped.decode()))
        if reply is not None:
            await stdin.write_object(reply)

    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            await take(line)
    await take(pending)


async def _forward_cli_errors(pump: SessionPump, stderr: anyio.abc.ByteReceiveStream) -> None:
    """Forward what the CLI wrote to stderr, to this log and to the console.

    stderr is the one place a failure to start is explained; without it the console sees only the
    selected harness exiting with status 1 for a rejected credential or a bad flag. Sent as
    `SetupOutput`, which is already "bytes the sandbox wrote".
    """
    async for chunk in stderr:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        await pump.stderr_output(chunk)


async def _start_cli(backend: CliBackend, launch: HarnessLaunch) -> anyio.abc.Process:
    resolved = backend.resolve(launch)
    return await anyio.open_process(
        resolved.command,
        cwd=resolved.cwd,
        env=resolved.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _launch_setup_path(launch: HarnessLaunch, fallback: Path | None) -> Path | None:
    """Select Agent-owned bootstrap from the launch, retaining an old-Console fallback."""
    if RUNNER_SETUP_ENV not in launch.environment:
        return fallback
    selected = launch.environment[RUNNER_SETUP_ENV]
    return Path(selected) if selected else None


def _write_runner_file(path: Path, content: str) -> None:
    """Write one launch-time runner file without following a stale-session symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            descriptor = -1
            stream.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _materialize_proxy_kubeconfig(launch: HarnessLaunch, bearer_token: str | None) -> HarnessLaunch:
    """Write a claim-local kubeconfig for Console's selected Kubernetes proxy.

    The bearer is intentionally stored in a mode-0600 tokenFile rather than in argv or kubeconfig
    YAML. It is already present by design in the ephemeral SandboxClaim environment for bridge and
    MCP authentication. The proxy URL is launch-selected so the runner does not carry a catalog of
    Console topology or bypass the authorization boundary.

    The proxy URL must be https: client-go reads kubeconfig user credentials only for a TLS
    server, so against a plain-http proxy kubectl sends every request unauthenticated and the
    proxy answers 401. The cluster entry pins the launch-selected sandbox trust bundle
    (`SSL_CERT_FILE`), which carries the internal root that signs the proxy's certificate.
    """
    proxy_url = launch.environment.get(KUBERNETES_PROXY_URL_ENV)
    if not proxy_url:
        return launch
    # The claim-owned credential always wins. Launch-selected environment is topology/options,
    # never authority, and must not be able to replace the bearer inherited by this runner Pod.
    token = bearer_token or os.environ.get(BRIDGE_CREDENTIAL_VARIABLE)
    if not token:
        raise RuntimeError(f"{KUBERNETES_PROXY_URL_ENV} requires a bridge bearer")

    home = Path(os.environ.get("HOME", "/home/runner"))
    kube_dir = home / ".kube"
    kube_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if kube_dir.is_symlink() or not kube_dir.is_dir():
        raise RuntimeError(f"refusing unsafe Kubernetes config directory {kube_dir}")
    kube_dir.chmod(0o700)
    token_path = kube_dir / "haku-agent-token"
    config_path = kube_dir / "config"
    cluster: dict[str, str] = {"server": proxy_url}
    if ca_bundle := launch.environment.get("SSL_CERT_FILE", os.environ.get("SSL_CERT_FILE", "")):
        cluster["certificate-authority"] = ca_bundle
    # JSON is valid kubeconfig YAML and avoids treating deploy-configured URL/path bytes as YAML.
    config = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "haku-console-proxy", "cluster": cluster}],
            "users": [{"name": "haku-agent-session", "user": {"tokenFile": str(token_path)}}],
            "contexts": [
                {
                    "name": "haku-agent-session",
                    "context": {"cluster": "haku-console-proxy", "user": "haku-agent-session"},
                }
            ],
            "current-context": "haku-agent-session",
        }
    )
    _write_runner_file(token_path, token)
    _write_runner_file(config_path, config)
    return launch.model_copy(update={"environment": {**launch.environment, "KUBECONFIG": str(config_path)}})


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
    process: anyio.abc.Process, pump: SessionPump, stdin: StdinWriter, scope: anyio.CancelScope
) -> None:
    """Read the CLI for as long as it lives, whether or not a console is listening.

    Long-lived rather than per-connection, which is what lets the process outlive a socket: the
    pipes keep draining into the pump's buffer, and when that buffer fills the reads stop and the
    CLI waits rather than losing what it said.

    Cancels *scope* when stdout ends, since that is the CLI exiting and there is then nothing left
    to serve any console with.
    """
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None
    assert stderr is not None
    async with anyio.create_task_group() as readers:
        # stderr ending says nothing about the conversation; stdout ending is the CLI's exit.
        readers.start_soon(_forward_cli_errors, pump, stderr)
        await _forward_cli_frames(pump, stdin, stdout)
        await pump.flushed()
        readers.cancel_scope.cancel()
    scope.cancel()


async def _serve_console(
    websocket: TextWebSocket, stdin: StdinWriter, outbound: MemoryObjectReceiveStream[str], pump: SessionPump
) -> None:
    """Copy both directions for one console connection, returning when that connection ends.

    The console's messages are commands now, not native input: journal ACKs release coalesced
    batches, prompt dispatches and interrupts are composed natively by the pump and written to the
    CLI here. Everything outbound was numbered where it happened and flows through one buffer, so
    this loop is a drain, not an author.
    """
    async with anyio.create_task_group() as tasks:

        async def console_to_cli() -> None:
            try:
                while True:
                    match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
                        case EndInput():
                            await stdin.aclose()
                        case HarnessFrame(frame=frame):
                            # The legacy console fold's native input. The journal console never
                            # sends one; accepting it keeps this end honest about what it was told.
                            await stdin.write_object(frame)
                        case ConsoleJournal(message=BatchAck() as ack):
                            await pump.acked(ack)
                        case ConsoleJournal(message=ConsoleResume()):
                            raise ValueError("console re-sent a journal resume mid-conversation")
                        case PromptDispatch() as dispatch:
                            payload = await pump.admit(dispatch)
                            if payload is not None:
                                await stdin.write_object(payload)
                        case Interrupt():
                            payload = await pump.interrupt()
                            if payload is not None:
                                await stdin.write_object(payload)
                        case HarnessLaunch():
                            # A sequencing error the types cannot express: `start` opens a
                            # connection, so a second one mid-conversation means the console
                            # thinks this runner never launched.
                            raise ValueError("console sent a second launch frame mid-conversation")
            except (EOFError, anyio.EndOfStream, ConnectionClosed):
                pass
            finally:
                tasks.cancel_scope.cancel()

        async def cli_to_console() -> None:
            try:
                async for text in outbound:
                    await websocket.send_text(text)
            except (ConnectionClosed, anyio.BrokenResourceError):
                pass
            finally:
                tasks.cancel_scope.cancel()

        tasks.start_soon(console_to_cli)
        tasks.start_soon(cli_to_console)


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

    **Fatal on failure.** Without the checkout the session has no manual, and a harness that starts
    anyway is silently a generic assistant.
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


def _narrator(websocket: TextWebSocket, pump: SessionPump) -> SetupNarration:
    """Send bootstrap output down *websocket*, numbered by *pump* like everything else this end
    sends. Direct rather than buffered: narration happens before the serve loop drains anything."""

    async def narrate(frame: SetupOutput) -> None:
        await pump.narration(websocket, frame)

    return narrate


async def _receive_launch(websocket: TextWebSocket) -> HarnessLaunch:
    """Say which versions this image speaks, then read the launch the console chose.

    The hello goes first on **every** connection, not only the first: a console adopting a session
    after a roll is a different process and has to be told the same thing.
    """
    await websocket.send_text(Hello().model_dump_json())
    if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), HarnessLaunch):
        raise ValueError(f"first bridge frame must be a launch, got {type(launch).__name__}")
    return launch


async def _handshake_journal(websocket: TextWebSocket) -> ConsoleResume:
    """Offer this image's generation and versions; read the console's resume — on every
    connection, because the durable batch cursor is exactly what a reconnect needs.

    A generation the console did not echo back is a console this runner must not serve: the
    maintenance-gated cut promises no old runner and new console (or the reverse) ever share a
    conversation, and this refusal is the runner's half of that promise.
    """
    await websocket.send_text(_journal_text(RunnerHello()))
    match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
        case ConsoleJournal(message=ConsoleResume() as resume):
            if resume.generation != GENERATION:
                raise ConsoleRefusedError(
                    f"transport generation mismatch: console={resume.generation!r} runner={GENERATION!r}"
                )
            return resume
        case other:
            raise ValueError(f"console answered the journal hello with {other.kind}")


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


def _process_stdin(process: anyio.abc.Process) -> anyio.abc.ByteSendStream:
    stdin = process.stdin
    assert stdin is not None
    return stdin


async def bridge_websocket_to_cli(websocket: TextWebSocket, *, backend: CliBackend, launch: HarnessLaunch) -> None:
    """Run one CLI and serve exactly one console connection with it.

    `run` composes the same pieces with a process that outlives the socket. No handshakes: the
    caller already owns both ends of this socket and hands the launch in directly; journal traffic
    still flows — batches out, ACKs in.
    """
    driver = backend.driver()
    launch = _materialize_proxy_kubeconfig(launch, os.environ.get(BRIDGE_CREDENTIAL_VARIABLE))
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[str](OUTBOUND_BUFFER)
    # No window, since there is no second connection to hand one to; still numbered, because the
    # console's log takes its ordering from that number either way.
    pump = SessionPump(driver, outbound_sender, window=0)
    pump.seed(launch.resume_from)
    process = await _start_cli(backend, launch)
    stdin = StdinWriter(_process_stdin(process))
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_drain_cli, process, pump, stdin, tasks.cancel_scope)
            await pump.initialized(stdin)
            await _serve_console(websocket, stdin, outbound_receiver, pump)
            tasks.cancel_scope.cancel()
    finally:
        exited_with = await _shutdown(process)
        await websocket.close()
    if exited_with not in (0, None):
        raise RuntimeError(f"{backend.name} exited with status {exited_with}")


async def run(
    websocket_url: str, backend: CliBackend, bearer_token: str | None, setup_path: Path | None = None
) -> None:
    """Serve one CLI to whichever console is up, across as many connections as that takes.

    **The CLI outlives the connection**, which is what keeps a console roll from ending the
    conversation. A later connection brings a freshly built `start` frame whose process fields are
    **ignored**: argv, system prompt and MCP wiring belong to a process already running and cannot
    be re-applied to it. What each connection genuinely brings is the two resume cursors — the
    console's frame cursor on `start`, its journal cursor on the `ConsoleResume` — and the replay
    each narrows.

    After `MAX_DISCONNECTED_SECONDS` with no console this exits and lets the claim be reclaimed;
    a console that refuses this runner outright (wrong generation, consumed credential, terminal
    session, journal violation) ends it at once.
    """
    # Before any dial: a harness without a neutral-operation driver cannot serve at this
    # generation, and the pod log should say so rather than a console see half a handshake.
    driver = backend.driver()
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[str](OUTBOUND_BUFFER)
    # Retained across connections: numbering, frame retention and journal retention are the
    # session's, however many consoles serve it.
    pump = SessionPump(driver, outbound_sender)
    process: anyio.abc.Process | None = None
    stdin: StdinWriter | None = None

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
                    pump.seed(launch.resume_from)
                    resume = await _handshake_journal(websocket)
                    # Replays go before live traffic on this socket. Duplicates with what the
                    # buffer still holds are expected and dropped by the console — frames by
                    # runner position, batches by idempotent commit.
                    for text in pump.missed(launch.resume_from):
                        await websocket.send_text(text)
                    for text in pump.resumed(resume):
                        await websocket.send_text(text)
                    if process is None:
                        # Materialize launch-owned Kubernetes configuration exactly once, before
                        # any bootstrap or harness code has had an opportunity to modify HOME.
                        launch = _materialize_proxy_kubeconfig(launch, bearer_token)
                        selected_setup = _launch_setup_path(launch, setup_path)
                        if selected_setup is not None:
                            await prepare_workspace(selected_setup, cwd=launch.cwd, narrate=_narrator(websocket, pump))
                        process = await _start_cli(backend, launch)
                        stdin = StdinWriter(_process_stdin(process))
                        # Long-lived, so nothing the CLI writes is lost to a closed socket: the
                        # pipes drain into the buffer either way, whose backpressure pauses the CLI
                        # rather than dropping what it said.
                        session.start_soon(_drain_cli, process, pump, stdin, session.cancel_scope)
                        await pump.initialized(stdin)
                    assert stdin is not None
                    await _serve_console(websocket, stdin, outbound_receiver, pump)
                except ConsoleRefusedError as refusal:
                    logger.warning("The console refused this runner (%s); releasing this sandbox", refusal)
                    break
                except ConnectionClosed as closed:
                    if (rcvd := closed.rcvd) is not None and rcvd.code == NOT_ADMITTED_CODE:
                        logger.warning(
                            "The console closed with policy code %s (%s); releasing this sandbox",
                            rcvd.code,
                            rcvd.reason,
                        )
                        break
                    # This connection ending says nothing about the session; `_dial` decides
                    # whether there is still a console worth waiting for.
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


def parse_args(backends: Mapping[str, BackendFactory]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge a Haku Console WebSocket to an agent CLI's stdio.")
    parser.add_argument("--websocket-url", default=os.environ.get("HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"))
    parser.add_argument("--session-id", default=os.environ.get("HAKU_RUNNER_SESSION_ID"))
    parser.add_argument(
        "--harness",
        choices=sorted(backends),
        default=os.environ.get("HAKU_HARNESS"),
        required=False,
        help="immutable native harness to run (the deployment must provide this explicitly)",
    )
    # Unset leaves the executable to the backend, which reads the variable its own image sets
    # (for example `claude_options.EXECUTABLE_VARIABLE`); this is for a local run against a CLI elsewhere.
    parser.add_argument("--cli-path", type=Path)
    # Unset means "no bootstrap", which is what tests and a bare local run want; the image sets it.
    # The bootstrap checks haku-state out and knows nothing about which CLI follows it.
    parser.add_argument("--setup-path", type=Path, default=_optional_path(os.environ.get(RUNNER_SETUP_ENV)))
    args = parser.parse_args()
    if not args.harness:
        parser.error("--harness or HAKU_HARNESS is required")
    if not args.websocket_url:
        parser.error("--websocket-url or HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL is required")
    if args.session_id:
        args.websocket_url = f"{args.websocket_url.rstrip('/')}/{args.session_id}"
    return args


def main() -> None:
    backends = runner_backends()
    args = parse_args(backends)
    anyio.run(
        run,
        args.websocket_url,
        backends[args.harness](args.cli_path),
        os.environ.get(BRIDGE_CREDENTIAL_VARIABLE),
        args.setup_path,
    )


if __name__ == "__main__":
    main()
