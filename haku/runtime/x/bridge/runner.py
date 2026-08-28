"""Sandbox bridge between a WebSocket and a local agent CLI, at the neutral-operation generation.

Which CLI it is stays behind the backend seam (<backend.py>); this module owns the
harness-invariant lifecycle — dial the console through the <communicator.py> `Communicator`, start
the backend's CLI, pump its stdio through the <session_api.py> `SessionPump`, and tear down when
the console gives up or the CLI exits. The pump numbers every stdout frame once, records it on the
wire as an opaque `HarnessFrame`, and folds it through the backend's `HarnessDriver` into the
neutral-operation journal (<neutral_operations.py>) that the Console commits and ACKs. The Console
writes no native input any more: it dispatches prompts by durable id (`PromptDispatch`) and asks
for interrupts (`Interrupt`); the runner composes the native frames, injects them, and echoes each
injection as a numbered `injected` frame so the durable record keeps both directions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from haku.runtime.x.bridge.backend import BRIDGE_CREDENTIAL_VARIABLE, CliBackend
from haku.runtime.x.bridge.backend_registry import BackendFactory, runner_backends
from haku.runtime.x.bridge.communicator import RECONNECT_BASE_DELAY, Communicator, ConsoleRefusedError
from haku.runtime.x.bridge.neutral_operations import BatchAck, ConsoleResume
from haku.runtime.x.bridge.protocol import (
    CONSOLE_TO_RUNNER,
    KUBERNETES_PROXY_URL_ENV,
    NOT_ADMITTED_CODE,
    RUNNER_SETUP_ENV,
    ConsoleJournal,
    EndInput,
    HarnessFrame,
    HarnessLaunch,
    Interrupt,
    PromptDispatch,
    SetupOutput,
    TextWebSocket,
    decode_object,
)
from haku.runtime.x.bridge.session_api import SessionPump, StdinWriter

logger = logging.getLogger(__name__)

# Where one line of bootstrap output goes. A callable rather than the websocket, so the frame still
# passes through the pump to be numbered: an unnumbered frame is a hole in the console's sequence.
SetupNarration = Callable[[SetupOutput], Awaitable[None]]

# What the CLI may say before its pipes fill and it pauses for want of a listener. Sized for a whole
# turn, since every streaming delta is forwarded and a long answer runs to thousands. Still a buffer
# rather than a store — what makes a reconnect lossless is the two resume cursors, not this number.
OUTBOUND_BUFFER = 10_000


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
    conversation. The <communicator.py> `Communicator` dials, handshakes and replays each console
    connection; this loop owns the process those connections serve — started once, on the first
    connection, and drained across every socket after.

    After `MAX_DISCONNECTED_SECONDS` with no console this exits and lets the claim be reclaimed;
    a console that refuses this runner outright (wrong generation, consumed credential, terminal
    session, journal violation) ends it at once.
    """
    # Before any dial: a harness without a neutral-operation driver cannot serve at this
    # generation, and the pod log should say so rather than a console see half a handshake.
    driver = backend.driver()
    communicator = Communicator(websocket_url, bearer_token)

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
                    websocket = await communicator.dial()
                except (OSError, InvalidHandshake) as error:
                    # The console refused this runner, or never came back inside
                    # `MAX_DISCONNECTED_SECONDS`; the error text says which. Both end the sandbox.
                    logger.info("Giving up on the console (%s); releasing this sandbox", error)
                    break
                try:
                    launch = await communicator.handshake(websocket, pump)
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
                    # This connection ending says nothing about the session; `dial` decides
                    # whether there is still a console worth waiting for.
                finally:
                    await websocket.close()
                # Not a backoff — `dial` owns that — but a floor, so a console that admits this
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
