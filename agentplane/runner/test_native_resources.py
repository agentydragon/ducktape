"""Measure pinned native harness residency and resume separately from runner journal bounds."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import serve
from agentplane.runner.testing import events
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class NativeSample:
    rss_kib: int
    peak_rss_kib: int
    cpu_ticks: int
    clock_ticks_per_second: int
    persistence_bytes: int
    persistence_files: int


def sample_native(pid: int, directory: Path) -> NativeSample:
    # Kernel process accounting, not the Python test/runner process. Never collect argv,
    # environment, file contents, or credentials in the profile artifacts.
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    sizes = [path.stat().st_size for path in directory.rglob("*") if path.is_file()]
    return NativeSample(
        rss_kib=int(status["VmRSS"].split()[0]),
        peak_rss_kib=int(status["VmHWM"].split()[0]),
        cpu_ticks=int(stat[11]) + int(stat[12]),
        clock_ticks_per_second=os.sysconf("SC_CLK_TCK"),
        persistence_bytes=sum(sizes),
        persistence_files=len(sizes),
    )


@asynccontextmanager
async def running_client(config: RunnerConfig) -> AsyncIterator[RunnerClient]:
    server, runner, port = await serve(config)
    client = RunnerClient(f"127.0.0.1:{port}")
    try:
        yield client
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)


@pytest.mark.parametrize("history_size", [1, 10, 100])
async def test_native_history_and_resume_resources(
    harness: protocol_pb2.Harness,
    config: RunnerConfig,
    model: ScriptedModel[Any],
    spec: protocol_pb2.SessionSpec,
    history_size: int,
) -> None:
    """The loopback upstream receives the real native request, including resumed history.

    This deliberately records measurements without asserting constant native memory or
    context. Agentplane's bounded journal does not control the native harness's context.
    """
    name = "claude" if harness == protocol_pb2.HARNESS_CLAUDE else "codex"
    session_id = "native-resource-profile"
    directory = config.state_dir / "sessions" / session_id / name
    inputs = [f"User marker {index}: " + "u" * 2048 for index in range(history_size + 1)]
    outputs = [f"Assistant marker {index}: " + "a" * 2048 for index in range(history_size + 1)]
    artifact = undeclared_outputs_dir() / f"native-{name}-{history_size}-resources.json"
    samples: dict[str, Any] = {"harness": name, "completed_turns": history_size, "text_bytes_per_marker": 2048}
    try:
        async with running_client(config) as client, await client.attach(session_id, spec=spec) as attachment:
            started = await attachment.until(events.is_kind("harness_started"))
            for index in range(history_size):
                await attachment.send(f"input-{index}", inputs[index])
                request = await model.request()
                assert request.user_texts == inputs[: index + 1]
                assert request.assistant_texts == outputs[:index]
                await model.reply(request, Text(outputs[index]))
                done = await attachment.until(events.turn_completed)
                assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
            samples["before_restart"] = asdict(sample_native(started.event.harness_started.pid, directory))
            cursor = attachment.cursor

        # New runner and new native process, using their durable directories. Do not replay
        # the runner archive into the test client or substitute synthetic native resume files.
        async with (
            running_client(config) as client,
            await client.attach(session_id, spec=spec, after_cursor=cursor) as attachment,
        ):
            started = await attachment.until(events.is_kind("harness_started"))
            assert started.event.harness_started.resumed
            await attachment.send("resumed-input", inputs[-1])
            request = await model.request()
            assert request.user_texts == inputs
            assert request.assistant_texts == outputs[:-1]
            samples["resumed_request"] = asdict(sample_native(started.event.harness_started.pid, directory))
            samples["resumed_user_messages"] = len(request.user_texts)
            samples["resumed_assistant_messages"] = len(request.assistant_texts)
            samples["resumed_conversation_utf8_bytes"] = sum(
                len(text.encode("utf-8")) for text in [*request.user_texts, *request.assistant_texts]
            )
            await model.reply(request, Text(outputs[-1]))
            done = await attachment.until(events.turn_completed)
            assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    finally:
        artifact.write_text(json.dumps(samples, indent=2) + "\n")


if __name__ == "__main__":
    pytest_bazel.main()
