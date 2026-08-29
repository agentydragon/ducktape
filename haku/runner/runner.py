"""Sandbox bridge between a WebSocket and a local agent CLI, at the neutral-operation generation.

This module owns only the harness-invariant lifecycle: dial the console through the
<communicator.py> `Communicator`, run the two handshakes and the roll replay, select the harness the
deployment named, start it once, serve whichever console is up, and release the sandbox when the
console gives up. It never inspects a native payload or drives a turn — the selected harness
(<backend.py> `Harness.run`) owns its binary, its protocol and its projection, and emits neutral
operations through the <session_api.py> `SessionApi` this loop hands it. The console composes no
native input: it dispatches prompts by durable id (`PromptDispatch`) and asks for interrupts
(`Interrupt`), which are handed straight to the harness's run-loop.
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

from haku.runner.backend import Harness, environment_session_token
from haku.runner.backend_registry import HarnessFactory, runner_harnesses
from haku.runner.communicator import RECONNECT_BASE_DELAY, Communicator, ConsoleRefusedError
from haku.runner.neutral_operations import BatchAck, ConsoleResume
from haku.runner.protocol import (
    CONSOLE_TO_RUNNER,
    KUBERNETES_PROXY_URL_ENV,
    NOT_ADMITTED_CODE,
    RUNNER_SETUP_ENV,
    ConsoleJournal,
    HarnessLaunch,
    Interrupt,
    PromptDispatch,
    SetupOutput,
    TextWebSocket,
)
from haku.runner.session_api import SessionApi

logger = logging.getLogger(__name__)

# Where one line of bootstrap output goes. A callable rather than the websocket, so the frame still
# passes through the session to be numbered: an unnumbered frame is a hole in the console's sequence.
SetupNarration = Callable[[SetupOutput], Awaitable[None]]

# What the CLI may say before its pipes fill and it pauses for want of a listener. Sized for a whole
# turn, since every streaming delta is forwarded and a long answer runs to thousands. Still a buffer
# rather than a store — what makes a reconnect lossless is the two resume cursors, not this number.
OUTBOUND_BUFFER = 10_000


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


def _materialize_proxy_kubeconfig(launch: HarnessLaunch, session_token: str | None) -> HarnessLaunch:
    """Write a claim-local kubeconfig for Console's selected Kubernetes proxy.

    The session token is intentionally stored in a mode-0600 tokenFile rather than in argv or
    kubeconfig YAML. It is already present by design in the ephemeral SandboxClaim environment for
    runner-protocol and MCP authentication. The proxy URL is launch-selected so the runner does not
    carry a catalog of Console topology or bypass the authorization boundary.

    The proxy URL must be https: client-go reads kubeconfig user credentials only for a TLS
    server, so against a plain-http proxy kubectl sends every request unauthenticated and the
    proxy answers 401. The cluster entry pins the launch-selected sandbox trust bundle
    (`SSL_CERT_FILE`), which carries the internal root that signs the proxy's certificate.
    """
    proxy_url = launch.environment.get(KUBERNETES_PROXY_URL_ENV)
    if not proxy_url:
        return launch
    # The claim-owned credential always wins. Launch-selected environment is topology/options,
    # never authority, and must not be able to replace the token inherited by this runner Pod.
    token = session_token or environment_session_token()
    if not token:
        raise RuntimeError(f"{KUBERNETES_PROXY_URL_ENV} requires a session token")

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


async def _serve_console(
    websocket: TextWebSocket, outbound: MemoryObjectReceiveStream[str], session: SessionApi
) -> None:
    """Copy both directions for one console connection, returning when that connection ends.

    The console's messages are commands, not native input: journal ACKs release coalesced batches
    here; prompt dispatches and interrupts are handed to the harness's run-loop, which composes the
    native input and journals the admission. Everything outbound was numbered where it happened and
    flows through one buffer, so this loop is a drain, not an author.
    """
    async with anyio.create_task_group() as tasks:

        async def console_to_harness() -> None:
            try:
                while True:
                    match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
                        case ConsoleJournal(message=BatchAck() as ack):
                            await session.acked(ack)
                        case ConsoleJournal(message=ConsoleResume()):
                            raise ValueError("console re-sent a journal resume mid-conversation")
                        case (PromptDispatch() | Interrupt()) as command:
                            await session.deliver(command)
                        case other:
                            # The neutral console composes no native input and opens exactly one
                            # connection with `start`; anything else here means it thinks this runner
                            # is a v3 peer it can hand native frames to.
                            raise ValueError(f"console sent an unexpected {other.kind} mid-conversation")
            except (EOFError, anyio.EndOfStream, ConnectionClosed):
                pass
            finally:
                tasks.cancel_scope.cancel()

        async def harness_to_console() -> None:
            try:
                async for text in outbound:
                    await websocket.send_text(text)
            except (ConnectionClosed, anyio.BrokenResourceError):
                pass
            finally:
                tasks.cancel_scope.cancel()

        tasks.start_soon(console_to_harness)
        tasks.start_soon(harness_to_console)


async def prepare_workspace(setup_path: Path, *, cwd: str, narrate: SetupNarration | None = None) -> None:
    """Run the shared sandbox bootstrap: git credentials and Haku's own checkouts.

    The same script the haku-sandbox exec target runs
    (<../../cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh>), so this box gets the
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


def _narrator(websocket: TextWebSocket, session: SessionApi) -> SetupNarration:
    """Send bootstrap output down *websocket*, numbered by *session* like everything else this end
    sends. Direct rather than buffered: narration happens before the serve loop drains anything."""

    async def narrate(frame: SetupOutput) -> None:
        await session.narration(websocket, frame)

    return narrate


async def _run_harness(harness: Harness, launch: HarnessLaunch, session: SessionApi, scope: anyio.CancelScope) -> None:
    """Run the selected harness for the session's life; its return or failure ends the session.

    Cancels *scope* when it returns — the harness's process has exited, so there is nothing left to
    serve any console with — and lets a nonzero-exit `RuntimeError` propagate to fail the session.
    """
    try:
        await harness.run(launch, session)
    finally:
        scope.cancel()


async def run(websocket_url: str, harness: Harness, session_token: str | None, setup_path: Path | None = None) -> None:
    """Serve one harness to whichever console is up, across as many connections as that takes.

    **The harness's CLI outlives the connection**, which is what keeps a console roll from ending
    the conversation. The <communicator.py> `Communicator` dials, handshakes and replays each
    console connection; this loop starts the harness once, on the first connection, and serves it
    across every socket after.

    After `MAX_DISCONNECTED_SECONDS` with no console this exits and lets the claim be reclaimed; a
    console that refuses this runner outright (wrong generation, consumed credential, terminal
    session, journal violation) ends it at once.
    """
    communicator = Communicator(websocket_url, session_token)
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[str](OUTBOUND_BUFFER)
    # Retained across connections: numbering, frame retention and journal retention are the
    # session's, however many consoles serve it.
    session = SessionApi(outbound_sender)
    launched = False

    async with anyio.create_task_group() as tasks:
        while True:
            try:
                websocket = await communicator.dial()
            except (OSError, InvalidHandshake) as error:
                # The console refused this runner, or never came back inside
                # `MAX_DISCONNECTED_SECONDS`; the error text says which. Both end the sandbox.
                logger.info("Giving up on the console (%s); releasing this sandbox", error)
                break
            try:
                launch = await communicator.handshake(websocket, session)
                if not launched:
                    # Materialize launch-owned Kubernetes configuration exactly once, before any
                    # bootstrap or harness code has had an opportunity to modify HOME.
                    launch = _materialize_proxy_kubeconfig(launch, session_token)
                    selected_setup = _launch_setup_path(launch, setup_path)
                    if selected_setup is not None:
                        await prepare_workspace(selected_setup, cwd=launch.cwd, narrate=_narrator(websocket, session))
                    tasks.start_soon(_run_harness, harness, launch, session, tasks.cancel_scope)
                    launched = True
                await _serve_console(websocket, outbound_receiver, session)
            except ConsoleRefusedError as refusal:
                logger.warning("The console refused this runner (%s); releasing this sandbox", refusal)
                break
            except ConnectionClosed as closed:
                if (rcvd := closed.rcvd) is not None and rcvd.code == NOT_ADMITTED_CODE:
                    logger.warning(
                        "The console closed with policy code %s (%s); releasing this sandbox", rcvd.code, rcvd.reason
                    )
                    break
                # This connection ending says nothing about the session; `dial` decides whether
                # there is still a console worth waiting for.
            finally:
                await websocket.close()
            # Not a backoff — `dial` owns that — but a floor, so a console that admits this runner
            # and immediately hangs up costs one redial a second, not a spin.
            await anyio.sleep(RECONNECT_BASE_DELAY)
        # Ends `_run_harness`, which otherwise holds this group open for as long as the CLI lives —
        # so giving up on the console would hang instead of releasing the sandbox.
        tasks.cancel_scope.cancel()


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def parse_args(harnesses: Mapping[str, HarnessFactory]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect a Haku Console WebSocket to an agent CLI's stdio.")
    # CLEANUP(added 2026-08-29): drop the HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL fallback once the
    # SandboxTemplates set only HAKU_RUNNER_WEBSOCKET_URL and no live sandbox predates that
    # manifest change — one release after the templates converge.
    parser.add_argument(
        "--websocket-url",
        default=os.environ.get("HAKU_RUNNER_WEBSOCKET_URL") or os.environ.get("HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"),
    )
    parser.add_argument("--session-id", default=os.environ.get("HAKU_RUNNER_SESSION_ID"))
    parser.add_argument(
        "--harness",
        choices=sorted(harnesses),
        default=os.environ.get("HAKU_HARNESS"),
        required=False,
        help="immutable native harness to run (the deployment must provide this explicitly)",
    )
    # Unset leaves the executable to the harness, which reads the variable its own image sets (for
    # example `claude.options.EXECUTABLE_VARIABLE`); this is for a local run against a CLI elsewhere.
    parser.add_argument("--cli-path", type=Path)
    # Unset means "no bootstrap", which is what tests and a bare local run want; the image sets it.
    # The bootstrap checks haku-state out and knows nothing about which CLI follows it.
    parser.add_argument("--setup-path", type=Path, default=_optional_path(os.environ.get(RUNNER_SETUP_ENV)))
    args = parser.parse_args()
    if not args.harness:
        parser.error("--harness or HAKU_HARNESS is required")
    if not args.websocket_url:
        parser.error("--websocket-url or HAKU_RUNNER_WEBSOCKET_URL is required")
    if args.session_id:
        args.websocket_url = f"{args.websocket_url.rstrip('/')}/{args.session_id}"
    return args


def main() -> None:
    harnesses = runner_harnesses()
    args = parse_args(harnesses)
    anyio.run(
        run, args.websocket_url, harnesses[args.harness](args.cli_path), environment_session_token(), args.setup_path
    )


if __name__ == "__main__":
    main()
