"""Compatibility tests for the executable Claude CLI bridge."""

from __future__ import annotations

import contextlib
from functools import partial
from http import HTTPStatus
from pathlib import Path

import anyio
import pytest
import pytest_bazel
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from haku.runtime.x.bridge.claude_options import ClaudeSession, HttpMcpServer, build_claude_launch, claude_backend
from haku.runtime.x.bridge.protocol import (
    FINE_GRAINED_TOOL_STREAMING_ENV,
    KUBERNETES_PROXY_URL_ENV,
    RUNNER_SETUP_ENV,
    RUNNER_TO_CONSOLE,
    EndInput,
    HarnessFrame,
    HarnessLaunch,
    SetupOutput,
)
from haku.runtime.x.bridge.runner import (
    Outbound,
    OutboundLog,
    _launch_setup_path,
    _materialize_proxy_kubeconfig,
    _narrator,
    _serve_console,
    _shutdown,
    _start_cli,
    bridge_websocket_to_cli,
    prepare_workspace,
    run,
)


class MemoryWebSocket:
    def __init__(self, *, incoming: anyio.abc.ObjectReceiveStream[str], outgoing: anyio.abc.ObjectSendStream[str]):
        self._incoming = incoming
        self._outgoing = outgoing
        self.closed = False

    async def send_text(self, data: str) -> None:
        await self._outgoing.send(data)

    async def receive_text(self) -> str:
        try:
            return await self._incoming.receive()
        except anyio.EndOfStream as error:
            raise EOFError from error

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._outgoing.aclose()
        await self._incoming.aclose()


def memory_websocket_pair() -> tuple[MemoryWebSocket, MemoryWebSocket]:
    left_to_right_send, left_to_right_receive = anyio.create_memory_object_stream[str](16)
    right_to_left_send, right_to_left_receive = anyio.create_memory_object_stream[str](16)
    return (
        MemoryWebSocket(incoming=right_to_left_receive, outgoing=left_to_right_send),
        MemoryWebSocket(incoming=left_to_right_receive, outgoing=right_to_left_send),
    )


def test_the_runner_runs_the_launch_the_console_sent(tmp_path: Path) -> None:
    """The sandbox side of the launch: the binary is the backend's to choose, everything after it
    is the console's.

    The argv itself is pinned in `test_claude_options.py`, and that the image carries an extracted
    `claude` is a property of the `claude_executable` genrule the image build exercises.
    """
    launch = build_claude_launch(
        ClaudeSession(
            cwd=tmp_path,
            permission_mode="dontAsk",
            append_system_prompt="remote prompt",
            mcp_servers={
                "haku-console": HttpMcpServer(
                    url="http://haku-console.test/mcp", headers={"Authorization": "Bearer test-static-agent-token"}
                )
            },
        )
    )

    process = claude_backend(Path("/usr/local/bin/claude")).resolve(launch)

    assert process.command == ["/usr/local/bin/claude", *launch.arguments]
    assert process.cwd == str(tmp_path)
    assert process.environment[FINE_GRAINED_TOOL_STREAMING_ENV] == "1"
    mcp_config = launch.arguments[launch.arguments.index("--mcp-config") + 1]
    assert "http://haku-console.test/mcp" in mcp_config
    assert "Bearer test-static-agent-token" in mcp_config


def test_environment_exposes_the_claim_owned_session_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv("HAKU_AGENT_SDK_RUNNER_TOKEN", "session-secret")
    monkeypatch.setenv("HAKU_MCP_BEARER_TOKEN", "session-secret")
    launch = HarnessLaunch(
        arguments=(),
        cwd="/workspace",
        environment={
            "CLAUDECODE": "injected-parent",
            "HAKU_AGENT_SDK_RUNNER_TOKEN": "injected-secret",
            "HAKU_MCP_BEARER_TOKEN": "injected-secret",
            "SAFE": "value",
        },
    )

    environment = claude_backend(Path("/usr/local/bin/claude")).resolve(launch).environment

    assert environment["CLAUDECODE"] == "injected-parent"
    assert environment["HAKU_AGENT_SDK_RUNNER_TOKEN"] == "session-secret"
    assert environment["HAKU_MCP_BEARER_TOKEN"] == "session-secret"
    assert environment["SAFE"] == "value"


def test_the_backend_names_the_binary_its_own_image_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-backend rather than one shared variable, which is what lets a second CLI arrive
    without renaming this one out from under the runner image and the SandboxTemplate."""
    monkeypatch.setenv("HAKU_CLAUDE_PATH", "/opt/claude")

    assert claude_backend().executable == Path("/opt/claude")
    assert claude_backend(Path("/elsewhere/claude")).executable == Path("/elsewhere/claude")


def test_proxy_kubeconfig_uses_token_file_and_never_serializes_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    launch = HarnessLaunch(
        arguments=(),
        cwd=str(tmp_path),
        environment={
            KUBERNETES_PROXY_URL_ENV: "https://haku-kube-api-proxy.haku-console:8443",
            "SSL_CERT_FILE": "/trust/ca-certificates.crt",
        },
    )

    materialized = _materialize_proxy_kubeconfig(launch, "session-secret")
    config = (tmp_path / ".kube/config").read_text()
    token = (tmp_path / ".kube/haku-agent-token").read_text()

    assert materialized.environment["KUBECONFIG"] == str(tmp_path / ".kube/config")
    assert "https://haku-kube-api-proxy.haku-console:8443" in config
    assert '"tokenFile":' in config
    # client-go attaches kubeconfig credentials only to a TLS server it can verify, so the
    # cluster entry pins the launch-selected sandbox trust bundle.
    assert '"certificate-authority": "/trust/ca-certificates.crt"' in config
    assert "session-secret" not in config
    assert token == "session-secret"
    assert (tmp_path / ".kube").stat().st_mode & 0o077 == 0
    assert (tmp_path / ".kube/haku-agent-token").stat().st_mode & 0o077 == 0
    assert (tmp_path / ".kube/config").stat().st_mode & 0o077 == 0


def test_proxy_kubeconfig_omits_certificate_authority_without_a_trust_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No launch or image bundle leaves verification to client-go's default pool."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    launch = HarnessLaunch(
        arguments=(),
        cwd=str(tmp_path),
        environment={KUBERNETES_PROXY_URL_ENV: "https://haku-kube-api-proxy.haku-console:8443"},
    )

    _materialize_proxy_kubeconfig(launch, "session-secret")

    assert "certificate-authority" not in (tmp_path / ".kube/config").read_text()


def test_proxy_kubeconfig_uses_claim_owned_bearer_not_launch_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    launch = HarnessLaunch(
        arguments=(),
        cwd=str(tmp_path),
        environment={
            KUBERNETES_PROXY_URL_ENV: "https://haku-kube-api-proxy.haku-console:8443",
            "HAKU_MCP_BEARER_TOKEN": "launch-selected-secret",
            "HAKU_AGENT_SDK_RUNNER_TOKEN": "launch-selected-secret",
        },
    )

    _materialize_proxy_kubeconfig(launch, "claim-owned-secret")

    assert (tmp_path / ".kube/haku-agent-token").read_text() == "claim-owned-secret"


def test_proxy_kubeconfig_rejects_a_stale_session_kube_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "old-session"
    target.mkdir()
    (tmp_path / ".kube").symlink_to(target, target_is_directory=True)
    launch = HarnessLaunch(
        arguments=(),
        cwd=str(tmp_path),
        environment={KUBERNETES_PROXY_URL_ENV: "https://haku-kube-api-proxy.haku-console:8443"},
    )

    with pytest.raises(RuntimeError, match="unsafe Kubernetes config directory"):
        _materialize_proxy_kubeconfig(launch, "new-session-secret")

    assert list(target.iterdir()) == []


def test_launch_setup_wins_over_old_image_fallback(tmp_path: Path) -> None:
    selected = tmp_path / "selected.sh"
    fallback = tmp_path / "old-image.sh"
    launch = HarnessLaunch(arguments=(), cwd=".", environment={RUNNER_SETUP_ENV: str(selected)})

    assert _launch_setup_path(launch, fallback) == selected
    assert _launch_setup_path(HarnessLaunch(arguments=(), cwd=".", environment={}), fallback) == fallback
    assert (
        _launch_setup_path(HarnessLaunch(arguments=(), cwd=".", environment={RUNNER_SETUP_ENV: ""}), fallback) is None
    )


async def test_bridge_copies_json_between_websocket_and_cli_stdio(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\nimport sys\nfor line in sys.stdin:\n    print(line, end='', flush=True)\n"
    )
    fake_claude.chmod(0o755)
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            partial(bridge_websocket_to_cli, runner_socket, backend=claude_backend(fake_claude), launch=launch)
        )
        message = {"type": "user", "message": {"role": "user", "content": "hello"}}
        await console_socket.send_text(HarnessFrame(frame=message).model_dump_json())
        with anyio.fail_after(5):
            # Unwrapped on the way to the CLI and re-wrapped on the way back, so the echo proves the
            # runner strips and restores the envelope. `seq=1` is the session's first frame.
            assert RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text()) == HarnessFrame(
                frame=message, seq=1
            )
        await console_socket.send_text(EndInput().model_dump_json())

    assert runner_socket.closed


async def test_what_the_cli_writes_to_stderr_reaches_the_console(tmp_path: Path) -> None:
    """The one place a CLI that fails to start explains itself: without it a rejected credential or
    a bad flag reaches the console as `claude exited with status 1` and nothing else."""
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('cannot start: no credential', file=sys.stderr, flush=True)\n"
    )
    fake_claude.chmod(0o755)
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            partial(bridge_websocket_to_cli, runner_socket, backend=claude_backend(fake_claude), launch=launch)
        )
        with anyio.fail_after(5):
            assert RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text()) == SetupOutput(
                data=b"cannot start: no credential\n", seq=1
            )


async def test_the_cli_keeps_running_when_a_console_connection_ends(tmp_path: Path) -> None:
    """The property roll survival rests on: `_serve_console` returning is this connection ending,
    not the conversation."""
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    fake_claude.chmod(0o755)
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](8)

    process = await _start_cli(claude_backend(fake_claude), launch)
    try:
        await console_socket.close()
        with anyio.fail_after(5):
            await _serve_console(runner_socket, process, outbound_receiver, OutboundLog())
        assert process.returncode is None, "the CLI must outlive the connection that was serving it"
    finally:
        outbound_sender.close()
        await _shutdown(process)


async def test_the_runner_waits_out_a_missing_console_but_not_a_refusing_one(tmp_path: Path) -> None:
    """The refusal a crashloop is made of, and the outage that is not one.

    All three of the console's refusal paths close before `accept()`, and an ASGI server answers
    such a handshake with `403`, never a close code — so a close-code check cannot fire. And
    `InvalidStatus` is not an `OSError`, so a single `503` from a Gateway with no ready backend —
    a console roll, from in here — must not escape `run()` and take the sandbox with it.
    """
    answered: list[int] = []

    def answer(connection: ServerConnection, request: Request) -> Response:
        status = HTTPStatus.SERVICE_UNAVAILABLE if not answered else HTTPStatus.FORBIDDEN
        answered.append(status)
        return connection.respond(status, "")

    async def never_reached(connection: ServerConnection) -> None:
        raise AssertionError("a rejected handshake must not reach the handler")

    async with serve(never_reached, host="127.0.0.1", port=0, process_request=answer) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        with anyio.fail_after(30):
            # Returns rather than raising: the sandbox is done, and a runner that exits nonzero
            # here is one Kubernetes restarts into the same refusal.
            await run(f"ws://127.0.0.1:{port}", claude_backend(tmp_path / "claude"), None)

    assert answered == [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.FORBIDDEN]


async def test_a_second_console_is_handed_what_the_first_may_not_have_recorded(tmp_path: Path) -> None:
    """The point of the window: nothing at this end can tell whether a frame handed to a dying
    socket was recorded, so it is offered again and the console dedupes by its runner position."""
    process = await _start_cli(claude_backend(executable(tmp_path / "claude", "sleep 30")), _launch(tmp_path))
    sender, receiver = anyio.create_memory_object_stream[Outbound](8)
    log = OutboundLog()
    payload = {"type": "assistant", "message": {"id": "msg_01abc"}}
    answered = HarnessFrame(frame=payload, seq=1).model_dump_json()
    try:
        first_console, first_runner = memory_websocket_pair()
        await sender.send(Outbound(frame=HarnessFrame(frame=payload)))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, first_runner, process, receiver, log))
            with anyio.fail_after(5):
                assert await first_console.receive_text() == answered
            await first_console.close()

        second_console, second_runner = memory_websocket_pair()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, second_runner, process, receiver, log))
            with anyio.fail_after(5):
                # The same text, `seq` included: a re-sent frame keeps the number it first went
                # out under, which is what lets two consoles agree on which frame it is.
                assert await second_console.receive_text() == answered, "the adopting console got nothing"
            await second_console.close()
    finally:
        sender.close()
        await _shutdown(process)


async def test_a_console_that_says_where_it_got_to_is_sent_only_what_it_is_missing(tmp_path: Path) -> None:
    """Catch-up, which is what the runner's own numbering is for.

    Without a cursor the adopting console is handed the whole window and dedupes it against its
    runner positions. With one, the runner answers from its own deque and the console is told
    exactly what it does not have.
    """
    process = await _start_cli(claude_backend(executable(tmp_path / "claude", "sleep 30")), _launch(tmp_path))
    sender, receiver = anyio.create_memory_object_stream[Outbound](8)
    log = OutboundLog()
    try:
        first_console, first_runner = memory_websocket_pair()
        for message_id in ("msg_01", "msg_02"):
            payload = {"type": "assistant", "message": {"id": message_id}}
            await sender.send(Outbound(frame=HarnessFrame(frame=payload)))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, first_runner, process, receiver, log))
            with anyio.fail_after(5):
                first = _claude_frame(await first_console.receive_text())
                second = _claude_frame(await first_console.receive_text())
            assert (first.seq, second.seq) == (1, 2), "the numbering must be dense, so a hole means loss"
            await first_console.close()

        # A console that recorded the first frame and died before the second.
        second_console, second_runner = memory_websocket_pair()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, second_runner, process, receiver, log, 1))
            with anyio.fail_after(5):
                resumed = _claude_frame(await second_console.receive_text())
            assert resumed.seq == 2, "a console holding frame 1 must be sent frame 2 and not frame 1"
            await second_console.close()
    finally:
        sender.close()
        await _shutdown(process)


def test_a_cursor_above_the_runners_own_count_lifts_it_rather_than_colliding() -> None:
    """A runner whose process restarted counts from 1 again, and the console's log does not.

    Seeding from the cursor is what keeps one session's frames one sequence: the next frame is
    numbered above everything already recorded rather than re-using numbers the log has spent.
    """
    log = OutboundLog()
    log.seed(41)
    stamped = log.stamp(Outbound(frame=SetupOutput(data=b"x")))
    assert RUNNER_TO_CONSOLE.validate_json(stamped) == SetupOutput(data=b"x", seq=42)
    assert log.missed(None) == [], "rendered setup narration cannot be replayed without duplicating lines"


async def test_a_delta_is_sent_and_replayed_by_position(tmp_path: Path) -> None:
    """Native deltas are opaque and retained like every other harness frame."""
    process = await _start_cli(claude_backend(executable(tmp_path / "claude", "sleep 30")), _launch(tmp_path))
    sender, receiver = anyio.create_memory_object_stream[Outbound](8)
    log = OutboundLog()
    payload = {"type": "stream_event", "event": {}}
    delta = HarnessFrame(frame=payload, seq=1).model_dump_json()
    try:
        console, runner = memory_websocket_pair()
        await sender.send(Outbound(frame=HarnessFrame(frame=payload)))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, runner, process, receiver, log))
            with anyio.fail_after(5):
                assert await console.receive_text() == delta, "a delta must still reach the console live"
            await console.close()

        assert log.missed(None), "a delta must be retained for position-based replay"
    finally:
        sender.close()
        await _shutdown(process)


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


def _launch(cwd: Path) -> HarnessLaunch:
    return HarnessLaunch(arguments=(), cwd=str(cwd), environment={})


def _claude_frame(text: str) -> HarnessFrame:
    frame = RUNNER_TO_CONSOLE.validate_json(text)
    assert isinstance(frame, HarnessFrame)
    return frame


async def test_workspace_setup_runs_in_the_launch_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = executable(tmp_path / "setup.sh", "pwd > marker")

    await prepare_workspace(setup, cwd=str(workspace))

    assert (workspace / "marker").read_text().strip() == str(workspace)


async def test_workspace_setup_streams_its_output_verbatim(tmp_path: Path) -> None:
    """The runner is a pipe: raw bytes, stderr included, no decoding and no line-splitting."""
    console_socket, runner_socket = memory_websocket_pair()
    # \xff is not valid UTF-8: a runner that decoded would replace it with U+FFFD before the
    # console ever saw it.
    setup = executable(tmp_path / "setup.sh", r"printf 'cloning\n\xff\n'" + "\necho 'trouble' >&2")

    await prepare_workspace(setup, cwd=str(tmp_path), narrate=_narrator(runner_socket, OutboundLog()))
    await runner_socket.close()

    forwarded = b""
    seqs: list[int | None] = []
    with contextlib.suppress(EOFError):
        while True:
            frame = RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text())
            assert isinstance(frame, SetupOutput)
            forwarded += frame.data
            seqs.append(frame.seq)
    assert forwarded == b"cloning\n\xff\ntrouble\n"
    # Narration is numbered from the same counter: it is the whole account of a session that died
    # before its first CLI frame, so leaving it out would put a hole at the very start.
    assert seqs == list(range(1, len(seqs) + 1))


async def test_workspace_setup_failure_is_fatal(tmp_path: Path) -> None:
    """No checkout means no manual, and a Claude that starts anyway is a generic assistant."""
    setup = executable(tmp_path / "setup.sh", "echo 'no credential' >&2; exit 3")

    with pytest.raises(RuntimeError, match="exited with status 3"):
        await prepare_workspace(setup, cwd=str(tmp_path))


if __name__ == "__main__":
    pytest_bazel.main()
