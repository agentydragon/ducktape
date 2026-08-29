"""The sandbox runner at the neutral-operation generation: launch resolution, the session's
numbering/journal/retention, and the Claude harness's process-level round trip.

The projection itself is `claude/test_projection.py` and the journal state machine is
`test_operation_journal.py`; here `SessionApi` is tested for what it adds on top — one dense
sequence over everything this end sends, native-input injection echoed into the record, the journal
batches riding the runner protocol envelope — and `ClaudeHarness.run` for starting the CLI, handshaking it,
and folding its echoes through the session.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
import pytest_bazel
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from haku.runner.backend import LEGACY_SESSION_TOKEN_VARIABLE, SESSION_TOKEN_VARIABLE, child_environment
from haku.runner.claude.harness import claude_harness
from haku.runner.claude.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runner.neutral_operations import (
    BatchAck,
    ConsoleResume,
    OperationBatch,
    PromptsCause,
    TurnAnswered,
    TurnEnded,
    TurnOpened,
    WakeCause,
)
from haku.runner.projection import Projected, at
from haku.runner.protocol import (
    FINE_GRAINED_TOOL_STREAMING_ENV,
    KUBERNETES_PROXY_URL_ENV,
    RUNNER_SETUP_ENV,
    RUNNER_TO_CONSOLE,
    HarnessFrame,
    HarnessLaunch,
    PromptDispatch,
    RunnerJournal,
    SetupOutput,
)
from haku.runner.runner import _launch_setup_path, _materialize_proxy_kubeconfig, _narrator, prepare_workspace, run
from haku.runner.session_api import SessionApi


def _no_answer(_payload: dict[str, Any]) -> None:
    return None


def _observe(*operations: Any) -> Callable[[int, dict[str, Any]], Projected]:
    def project(_seq: int, _payload: dict[str, Any]) -> Projected:
        return Projected(operations=operations, unprojected={})

    return project


def _admission(*operations: Any) -> Callable[..., Projected]:
    def project(*, after_batch_seq: int | None, frame_seq: int | None) -> Projected:
        return Projected(operations=operations, unprojected={})

    return project


async def _drain(receiver: anyio.abc.ObjectReceiveStream[str]) -> list[str]:
    """Everything the session has put on the wire so far — the timeout is how "so far" ends, since
    the stream stays open."""
    out: list[str] = []
    with anyio.move_on_after(0.2):
        while True:
            out.append(await receiver.receive())
    return out


def _journal_batches(texts: list[str]) -> list[OperationBatch]:
    batches: list[OperationBatch] = []
    for text in texts:
        message = RUNNER_TO_CONSOLE.validate_json(text)
        if isinstance(message, RunnerJournal) and isinstance(message.message, OperationBatch):
            batches.append(message.message)
    return batches


# --- launch resolution ---


def test_the_runner_runs_the_launch_the_console_sent(tmp_path: Path) -> None:
    """The sandbox side of the launch: the binary is the harness's to choose, everything after it
    is the console's. The argv itself is pinned in `claude/test_options.py`."""
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
    process = claude_harness(Path("/usr/local/bin/claude")).resolve(launch)
    assert process.command == ["/usr/local/bin/claude", *launch.arguments]
    assert process.cwd == str(tmp_path)
    assert process.environment[FINE_GRAINED_TOOL_STREAMING_ENV] == "1"
    mcp_config = launch.arguments[launch.arguments.index("--mcp-config") + 1]
    assert "http://haku-console.test/mcp" in mcp_config
    assert "Bearer test-static-agent-token" in mcp_config


def test_environment_exposes_the_claim_owned_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv(SESSION_TOKEN_VARIABLE, "session-secret")
    monkeypatch.setenv(LEGACY_SESSION_TOKEN_VARIABLE, "session-secret")
    launch = HarnessLaunch(
        arguments=(),
        cwd="/workspace",
        environment={
            "CLAUDECODE": "injected-parent",
            SESSION_TOKEN_VARIABLE: "injected-secret",
            LEGACY_SESSION_TOKEN_VARIABLE: "injected-secret",
            "SAFE": "value",
        },
    )
    environment = claude_harness(Path("/usr/local/bin/claude")).resolve(launch).environment
    assert environment["CLAUDECODE"] == "injected-parent"
    assert environment[SESSION_TOKEN_VARIABLE] == "session-secret"
    assert environment[LEGACY_SESSION_TOKEN_VARIABLE] == "session-secret"
    assert environment["SAFE"] == "value"


def test_environment_uses_the_session_token_for_proxy_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSION_TOKEN_VARIABLE, "session bearer/with spaces")
    launch = HarnessLaunch(
        arguments=(),
        cwd="/workspace",
        environment={
            "HTTP_PROXY": "http://egress-proxy.test:8888",
            "HTTPS_PROXY": "https://egress-proxy.test:8443",
            "NO_PROXY": "localhost",
        },
    )

    environment = child_environment(launch)

    assert environment["HTTP_PROXY"] == "http://:session%20bearer%2Fwith%20spaces@egress-proxy.test:8888"
    assert environment["HTTPS_PROXY"] == "https://:session%20bearer%2Fwith%20spaces@egress-proxy.test:8443"
    assert environment["NO_PROXY"] == "localhost"


def test_proxy_authentication_falls_back_to_the_legacy_token_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim minted before the HAKU_SESSION_TOKEN rename carries only the legacy variable."""
    monkeypatch.delenv(SESSION_TOKEN_VARIABLE, raising=False)
    monkeypatch.setenv(LEGACY_SESSION_TOKEN_VARIABLE, "legacy-token")
    launch = HarnessLaunch(arguments=(), cwd="/workspace", environment={"HTTP_PROXY": "http://egress-proxy.test:8888"})
    assert child_environment(launch)["HTTP_PROXY"] == "http://:legacy-token@egress-proxy.test:8888"


def test_the_harness_names_the_binary_its_own_image_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAKU_CLAUDE_PATH", "/opt/claude")
    assert claude_harness().executable == Path("/opt/claude")
    assert claude_harness(Path("/elsewhere/claude")).executable == Path("/elsewhere/claude")


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
            SESSION_TOKEN_VARIABLE: "launch-selected-secret",
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


# --- SessionApi: one sequence, journal batches, injection echoes, retention ---


async def test_the_session_numbers_everything_it_sends_on_one_dense_sequence() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    session = SessionApi(sender, window=8)
    await session.observe(
        {"type": "assistant"}, _observe(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=at(1))), _no_answer
    )
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
    session = SessionApi(sender, window=8)
    prompt_id = uuid4()
    native = {"type": "user", "content": "hello"}
    payload = await session.admit(
        prompt_id,
        lambda: native,
        _admission(TurnOpened(turn_id=uuid4(), cause=PromptsCause(prompt_ids=(prompt_id,)), provenance=at(1))),
    )
    assert payload == native, "the native input to write to the CLI"
    sent = await _drain(receiver)
    echo = RUNNER_TO_CONSOLE.validate_json(sent[0])
    assert isinstance(echo, HarnessFrame)
    assert echo.injected, "the injection is recorded to_agent"
    assert echo.frame == native
    assert isinstance(RUNNER_TO_CONSOLE.validate_json(sent[1]), RunnerJournal), "the admission was journalled"


async def test_a_duplicate_dispatch_is_ignored() -> None:
    sender, _ = anyio.create_memory_object_stream[str](32)
    session = SessionApi(sender, window=8)
    prompt_id = uuid4()
    assert await session.admit(prompt_id, lambda: {"type": "user"}, _admission()) is not None
    assert await session.admit(prompt_id, lambda: {"type": "user"}, _admission()) is None, (
        "the runner ignores an id it took"
    )


async def test_an_interrupt_rewrites_the_next_turn_end_to_aborted() -> None:
    turn_id = uuid4()
    sender, receiver = anyio.create_memory_object_stream[str](32)
    session = SessionApi(sender, window=8)
    await session.interrupt(lambda: {"type": "interrupt"})
    await session.observe(
        {"type": "result"}, _observe(TurnEnded(turn_id=turn_id, end=TurnAnswered(), provenance=at(2))), _no_answer
    )
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
    session = SessionApi(sender, window=8)
    await session.observe(
        {"n": 1}, _observe(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=at(1))), _no_answer
    )  # cuts batch 1 (nothing in flight)
    await session.observe(
        {"n": 2}, _observe(TurnEnded(turn_id=uuid4(), end=TurnAnswered(), provenance=at(2))), _no_answer
    )  # coalesces behind the unacked batch 1
    before_ack = _journal_batches(await _drain(receiver))
    assert [batch.runner_batch_seq for batch in before_ack] == [1], "only the first batch went; the second is held"
    await session.acked(BatchAck(acked_batch_seq=1))
    after_ack = _journal_batches(await _drain(receiver))
    assert [batch.runner_batch_seq for batch in after_ack] == [2], "the ACK released the coalesced batch"


async def test_resume_replays_retained_batches_above_the_cursor() -> None:
    sender, receiver = anyio.create_memory_object_stream[str](32)
    session = SessionApi(sender, window=8)
    await session.observe(
        {"n": 1}, _observe(TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=at(1))), _no_answer
    )
    await _drain(receiver)
    replay = _journal_batches(session.resumed(ConsoleResume(neutral_protocol_version=1, acked_batch_seq=None)))
    assert [batch.runner_batch_seq for batch in replay] == [1], "a console with nothing acked is replayed the batch"


# --- Claude harness: process-level round trip ---


def _fake_claude(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{body}")
    path.chmod(0o755)
    return path


async def test_the_harness_journals_a_message_the_cli_echoes(tmp_path: Path) -> None:
    """A dispatched prompt is composed and written to the CLI, the CLI's echo is recorded and
    projected, and both the injected frame and the observed one reach the console."""
    fake = _fake_claude(
        tmp_path / "claude",
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    json.loads(line)\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'id': 'msg_1', 'content': []}}), flush=True)\n",
    )
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    sender, receiver = anyio.create_memory_object_stream[str](64)
    session = SessionApi(sender)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(claude_harness(fake).run, launch, session))
        await session.deliver(PromptDispatch(prompt_id=uuid4(), text="hello"))
        seen_injected = seen_output = False
        with anyio.fail_after(10):
            while not (seen_injected and seen_output):
                message = RUNNER_TO_CONSOLE.validate_json(await receiver.receive())
                if isinstance(message, HarnessFrame) and message.injected:
                    seen_injected = True  # the initialize or the prompt the runner injected
                elif isinstance(message, HarnessFrame) and not message.injected:
                    seen_output = True  # the CLI's echoed assistant frame, recorded from_agent
        tasks.cancel_scope.cancel()


async def test_what_the_cli_writes_to_stderr_reaches_the_console(tmp_path: Path) -> None:
    """The one place a CLI that fails to start explains itself, forwarded as numbered narration."""
    fake = _fake_claude(
        tmp_path / "claude",
        "import sys\nprint('cannot start: no credential', file=sys.stderr, flush=True)\nsys.stdin.read()\n",
    )
    launch = HarnessLaunch(arguments=(), cwd=str(tmp_path), environment={})
    sender, receiver = anyio.create_memory_object_stream[str](64)
    session = SessionApi(sender)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(claude_harness(fake).run, launch, session))
        with anyio.fail_after(10):
            while True:
                message = RUNNER_TO_CONSOLE.validate_json(await receiver.receive())
                if isinstance(message, SetupOutput):
                    assert message.data == b"cannot start: no credential\n"
                    break
        tasks.cancel_scope.cancel()


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
            await run(f"ws://127.0.0.1:{port}", claude_harness(tmp_path / "claude"), None)
    assert answered == [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.FORBIDDEN]


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


async def test_workspace_setup_runs_in_the_launch_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = _executable(tmp_path / "setup.sh", "pwd > marker")
    await prepare_workspace(setup, cwd=str(workspace))
    assert (workspace / "marker").read_text().strip() == str(workspace)


async def test_workspace_setup_streams_its_output_verbatim(tmp_path: Path) -> None:
    """The runner is a pipe: raw bytes, stderr included, no decoding and no line-splitting."""
    outgoing_send, outgoing_receive = anyio.create_memory_object_stream[str](32)
    sender, _ = anyio.create_memory_object_stream[str](8)
    session = SessionApi(sender, window=0)
    setup = _executable(tmp_path / "setup.sh", r"printf 'cloning\n\xff\n'" + "\necho 'trouble' >&2")
    socket = _CollectingWebSocket(outgoing_send)

    await prepare_workspace(setup, cwd=str(tmp_path), narrate=_narrator(socket, session))
    await outgoing_send.aclose()

    forwarded = b""
    seqs: list[int | None] = []
    async for text in outgoing_receive:
        frame = RUNNER_TO_CONSOLE.validate_json(text)
        assert isinstance(frame, SetupOutput)
        forwarded += frame.data
        seqs.append(frame.seq)
    assert forwarded == b"cloning\n\xff\ntrouble\n"
    assert seqs == list(range(1, len(seqs) + 1)), "narration shares the one dense sequence from its start"


async def test_workspace_setup_failure_is_fatal(tmp_path: Path) -> None:
    setup = _executable(tmp_path / "setup.sh", "echo 'no credential' >&2; exit 3")
    with pytest.raises(RuntimeError, match="exited with status 3"):
        await prepare_workspace(setup, cwd=str(tmp_path))


class _CollectingWebSocket:
    """A `TextWebSocket` that forwards each sent frame to a memory stream, for narration tests."""

    def __init__(self, outgoing: anyio.abc.ObjectSendStream[str]):
        self._outgoing = outgoing

    async def send_text(self, data: str) -> None:
        await self._outgoing.send(data)

    async def receive_text(self) -> str:
        raise AssertionError("narration never reads")

    async def close(self) -> None:
        await self._outgoing.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
