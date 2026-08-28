"""The sandbox bridge at the neutral-operation generation: launch resolution, the session pump's
numbering/journal/retention, and the process-level round trip.

The projection itself is `test_claude_projection.py` and the journal state machine is
`test_operation_journal.py`; here the `SessionPump` is tested for what it adds on top — one dense
sequence over everything this end sends, native-input injection echoed into the record, and the
journal batches riding the bridge envelope.
"""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest
import pytest_bazel
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from haku.runtime.x.bridge.claude_options import ClaudeSession, HttpMcpServer, build_claude_launch, claude_backend
from haku.runtime.x.bridge.claude_projection import Projected
from haku.runtime.x.bridge.neutral_operations import (
    BatchAck,
    ConsoleResume,
    FrameRange,
    OperationBatch,
    TurnAnswered,
    TurnEnded,
    TurnOpened,
    WakeCause,
)
from haku.runtime.x.bridge.protocol import (
    FINE_GRAINED_TOOL_STREAMING_ENV,
    KUBERNETES_PROXY_URL_ENV,
    RUNNER_SETUP_ENV,
    RUNNER_TO_CONSOLE,
    EndInput,
    HarnessFrame,
    HarnessLaunch,
    PromptDispatch,
    RunnerJournal,
    SetupOutput,
)
from haku.runtime.x.bridge.runner import (
    SessionPump,
    _launch_setup_path,
    _materialize_proxy_kubeconfig,
    _narrator,
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
    left_to_right_send, left_to_right_receive = anyio.create_memory_object_stream[str](32)
    right_to_left_send, right_to_left_receive = anyio.create_memory_object_stream[str](32)
    return (
        MemoryWebSocket(incoming=right_to_left_receive, outgoing=left_to_right_send),
        MemoryWebSocket(incoming=left_to_right_receive, outgoing=right_to_left_send),
    )


class _FakeDriver:
    """A `HarnessDriver` whose meaning is scripted per test, and whose composition is inspectable."""

    def __init__(self, *, observe_yields: dict[int, Projected] | None = None):
        self._observe_yields = observe_yields or {}
        self.composed: list[dict] = []

    def initialize(self) -> dict | None:
        return None

    def compose_prompt(self, text: str) -> dict:
        payload = {"type": "user", "content": text}
        self.composed.append(payload)
        return payload

    def compose_interrupt(self) -> dict | None:
        payload = {"type": "interrupt"}
        self.composed.append(payload)
        return payload

    def answer_control_request(self, payload: dict) -> dict | None:
        return None

    def observe(self, frame_seq: int, payload: dict) -> Projected:
        return self._observe_yields.get(frame_seq, Projected(operations=(), unprojected={}))

    def admit(self, prompt_id: UUID, *, after_batch_seq: int | None, frame_seq: int | None) -> Projected:
        turn = TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=None)
        return Projected(operations=(turn,), unprojected={})


def _at(seq: int) -> FrameRange:
    return FrameRange(first_frame_seq=seq, last_frame_seq=seq)


async def _drain(receiver: anyio.abc.ObjectReceiveStream[str]) -> list[str]:
    """Everything the pump has put on the wire so far — the timeout is how "so far" ends, since the
    stream stays open."""
    out: list[str] = []
    with anyio.move_on_after(0.2):
        while True:
            out.append(await receiver.receive())
    return out


def test_the_runner_runs_the_launch_the_console_sent(tmp_path: Path) -> None:
    """The sandbox side of the launch: the binary is the backend's to choose, everything after it
    is the console's. The argv itself is pinned in `test_claude_options.py`."""
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


def test_the_backend_carries_a_neutral_operation_driver() -> None:
    """A harness serves at this generation only if it can interpret its own frames: the backend
    hands the runner a driver, and a runner without one never launches a CLI."""
    driver = claude_backend(Path("/usr/local/bin/claude")).driver()
    assert driver.compose_prompt("hi")["message"]["content"] == "hi"


def test_environment_exposes_the_claim_owned_session_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv("HAKU_AGENT_SDK_RUNNER_TOKEN", "session-secret")
    launch = HarnessLaunch(
        arguments=(),
        cwd="/workspace",
        environment={
            "CLAUDECODE": "injected-parent",
            "HAKU_AGENT_SDK_RUNNER_TOKEN": "injected-secret",
            "SAFE": "value",
        },
    )
    environment = claude_backend(Path("/usr/local/bin/claude")).resolve(launch).environment
    assert environment["CLAUDECODE"] == "injected-parent"
    assert environment["HAKU_AGENT_SDK_RUNNER_TOKEN"] == "session-secret"
    assert environment["SAFE"] == "value"


def test_the_backend_names_the_binary_its_own_image_set(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert '"certificate-authority": "/trust/ca-certificates.crt"' in config
    assert "session-secret" not in config
    assert token == "session-secret"
    assert (tmp_path / ".kube").stat().st_mode & 0o077 == 0
    assert (tmp_path / ".kube/haku-agent-token").stat().st_mode & 0o077 == 0
    assert (tmp_path / ".kube/config").stat().st_mode & 0o077 == 0


def test_proxy_kubeconfig_omits_certificate_authority_without_a_trust_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


# --- SessionPump: one sequence, journal batches, injection echoes, retention ---


async def test_the_pump_numbers_everything_it_sends_on_one_dense_sequence() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    driver = _FakeDriver(
        observe_yields={
            1: Projected(
                operations=(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=_at(1)),), unprojected={}
            )
        }
    )
    pump = SessionPump(driver, sender, window=8)
    await pump.observed({"type": "assistant"})
    sent = await _drain(receiver)
    frame = RUNNER_TO_CONSOLE.validate_json(sent[0])
    assert isinstance(frame, HarnessFrame)
    assert frame.seq == 1
    assert not frame.injected
    assert isinstance(RUNNER_TO_CONSOLE.validate_json(sent[1]), RunnerJournal), (
        "the projected operation rode as a batch"
    )


async def test_a_dispatched_prompt_is_injected_echoed_and_journalled() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    driver = _FakeDriver()
    pump = SessionPump(driver, sender, window=8)
    prompt_id = uuid4()
    payload = await pump.admit(PromptDispatch(prompt_id=prompt_id, text="hello"))
    assert payload == {"type": "user", "content": "hello"}, "the native input to write to the CLI"
    sent = await _drain(receiver)
    echo = RUNNER_TO_CONSOLE.validate_json(sent[0])
    assert isinstance(echo, HarnessFrame)
    assert echo.injected, "the injection is recorded to_agent"
    assert echo.frame == payload
    assert isinstance(RUNNER_TO_CONSOLE.validate_json(sent[1]), RunnerJournal), "the admission was journalled"


async def test_a_duplicate_dispatch_is_ignored() -> None:
    sender, _ = anyio.create_memory_object_stream[str](32)
    pump = SessionPump(_FakeDriver(), sender, window=8)
    prompt_id = uuid4()
    assert await pump.admit(PromptDispatch(prompt_id=prompt_id, text="hi")) is not None
    assert await pump.admit(PromptDispatch(prompt_id=prompt_id, text="hi")) is None, "the runner ignores an id it took"


async def test_an_interrupt_rewrites_the_next_turn_end_to_aborted() -> None:
    turn_id = uuid4()
    sender, receiver = anyio.create_memory_object_stream[str](32)
    driver = _FakeDriver(
        observe_yields={
            2: Projected(
                operations=(TurnEnded(turn_id=turn_id, end=TurnAnswered(), provenance=_at(2)),), unprojected={}
            )
        }
    )
    pump = SessionPump(driver, sender, window=8)
    await pump.interrupt()
    await pump.observed({"type": "result"})
    sent = await _drain(receiver)
    ended = [
        op
        for text in sent
        if isinstance(msg := RUNNER_TO_CONSOLE.validate_json(text), RunnerJournal)
        and isinstance(msg.message, OperationBatch)
        for op in msg.message.operations
        if isinstance(op, TurnEnded)
    ]
    assert len(ended) == 1
    assert ended[0].end.outcome == "aborted", "the side that asked records the abort"


async def test_the_journal_coalesces_behind_an_unacked_batch_and_releases_on_ack() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    driver = _FakeDriver(
        observe_yields={
            1: Projected(
                operations=(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=_at(1)),), unprojected={}
            ),
            2: Projected(
                operations=(TurnEnded(turn_id=uuid4(), end=TurnAnswered(), provenance=_at(2)),), unprojected={}
            ),
        }
    )
    pump = SessionPump(driver, sender, window=8)
    await pump.observed({"n": 1})  # cuts batch 1 (nothing in flight)
    await pump.observed({"n": 2})  # coalesces behind the unacked batch 1
    before_ack = _journal_batches(await _drain(receiver))
    assert [batch.runner_batch_seq for batch in before_ack] == [1], "only the first batch went; the second is held"
    await pump.acked(BatchAck(acked_batch_seq=1))
    after_ack = _journal_batches(await _drain(receiver))
    assert [batch.runner_batch_seq for batch in after_ack] == [2], "the ACK released the coalesced batch"


async def test_resume_replays_retained_batches_above_the_cursor() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    driver = _FakeDriver(
        observe_yields={
            1: Projected(
                operations=(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=_at(1)),), unprojected={}
            )
        }
    )
    pump = SessionPump(driver, sender, window=8)
    await pump.observed({"n": 1})
    await _drain(receiver)
    replay = _journal_batches(pump.resumed(ConsoleResume(neutral_protocol_version=1, acked_batch_seq=None)))
    assert [batch.runner_batch_seq for batch in replay] == [1], "a console with nothing acked is replayed the batch"


def _journal_batches(texts: list[str]) -> list[OperationBatch]:
    batches: list[OperationBatch] = []
    for text in texts:
        message = RUNNER_TO_CONSOLE.validate_json(text)
        if isinstance(message, RunnerJournal) and isinstance(message.message, OperationBatch):
            batches.append(message.message)
    return batches


# --- process-level round trip ---


async def test_the_runner_waits_out_a_missing_console_but_not_a_refusing_one(tmp_path: Path) -> None:
    """The refusal a crashloop is made of, and the outage that is not one — decided from the
    handshake status, never a close code, because a pre-`accept()` refusal is an HTTP response."""
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
            await run(f"ws://127.0.0.1:{port}", claude_backend(tmp_path / "claude"), None)
    assert answered == [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.FORBIDDEN]


async def test_the_bridge_journals_a_message_the_cli_echoes(tmp_path: Path) -> None:
    """One process, one connection: a dispatched prompt is written to the CLI, the CLI's echo is
    recorded and projected, and both the frame and its journal batch reach the console."""
    fake_claude = tmp_path / "claude"
    # Echoes each stdin line back on stdout as one assistant frame, so the runner has something to
    # observe and journal.
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    obj = json.loads(line)\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'id': 'msg_1', 'content': []}}), flush=True)\n"
    )
    fake_claude.chmod(0o755)
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            partial(bridge_websocket_to_cli, runner_socket, backend=claude_backend(fake_claude), launch=launch)
        )
        await console_socket.send_text(PromptDispatch(prompt_id=uuid4(), text="hello").model_dump_json())
        seen_injected = seen_output = False
        with anyio.fail_after(10):
            while not (seen_injected and seen_output):
                message = RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text())
                if isinstance(message, HarnessFrame) and message.injected:
                    seen_injected = True  # the prompt the runner composed and injected
                elif isinstance(message, HarnessFrame) and not message.injected:
                    seen_output = True  # the CLI's echoed assistant frame, recorded from_agent
        await console_socket.send_text(EndInput().model_dump_json())
    assert runner_socket.closed


async def test_what_the_cli_writes_to_stderr_reaches_the_console(tmp_path: Path) -> None:
    """The one place a CLI that fails to start explains itself, forwarded as numbered narration."""
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
        with anyio.fail_after(10):
            while True:
                message = RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text())
                if isinstance(message, SetupOutput):
                    assert message.data == b"cannot start: no credential\n"
                    break
        await console_socket.close()


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


async def test_workspace_setup_runs_in_the_launch_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = executable(tmp_path / "setup.sh", "pwd > marker")
    await prepare_workspace(setup, cwd=str(workspace))
    assert (workspace / "marker").read_text().strip() == str(workspace)


async def test_workspace_setup_streams_its_output_verbatim(tmp_path: Path) -> None:
    """The runner is a pipe: raw bytes, stderr included, no decoding and no line-splitting."""
    console_socket, runner_socket = memory_websocket_pair()
    sender, _ = anyio.create_memory_object_stream[str](8)
    pump = SessionPump(_FakeDriver(), sender, window=0)
    setup = executable(tmp_path / "setup.sh", r"printf 'cloning\n\xff\n'" + "\necho 'trouble' >&2")

    await prepare_workspace(setup, cwd=str(tmp_path), narrate=_narrator(runner_socket, pump))
    await runner_socket.close()

    forwarded = b""
    seqs: list[int | None] = []
    try:
        while True:
            frame = RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text())
            assert isinstance(frame, SetupOutput)
            forwarded += frame.data
            seqs.append(frame.seq)
    except EOFError:
        pass
    assert forwarded == b"cloning\n\xff\ntrouble\n"
    assert seqs == list(range(1, len(seqs) + 1)), "narration shares the one dense sequence from its start"


async def test_workspace_setup_failure_is_fatal(tmp_path: Path) -> None:
    setup = executable(tmp_path / "setup.sh", "echo 'no credential' >&2; exit 3")
    with pytest.raises(RuntimeError, match="exited with status 3"):
        await prepare_workspace(setup, cwd=str(tmp_path))


if __name__ == "__main__":
    pytest_bazel.main()
