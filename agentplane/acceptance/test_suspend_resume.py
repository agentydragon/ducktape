"""Completed conversations survive a real Sandbox suspend/resume on both harnesses."""

from __future__ import annotations

import asyncio
import json
import subprocess
from uuid import UUID, uuid4

import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.agent import TURN_SECONDS, Agent
from agentplane.acceptance.conftest import Sandboxes, wait_until_running
from agentplane.app.testing.client import Client
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import OperatingMode
from util.testing.undeclared_outputs import undeclared_outputs_dir

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

POD_TRANSITION_SECONDS = 300.0


def _state_volume(namespace: str, sandbox_uid: UUID) -> tuple[str, str]:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "pvc", "-o", "json"], capture_output=True, text=True, check=True, timeout=20
    )
    claims = [
        claim
        for claim in json.loads(result.stdout)["items"]
        if any(
            owner.get("controller") and owner["uid"] == str(sandbox_uid)
            for owner in claim["metadata"].get("ownerReferences", [])
        )
    ]
    assert len(claims) == 1, f"expected one Sandbox-owned state PVC, found {len(claims)}"
    claim = claims[0]
    assert claim["status"]["phase"] == "Bound"
    return claim["metadata"]["uid"], claim["spec"]["volumeName"]


async def _history(client: Client, thread_id: UUID, *, completed_turns: int | None) -> list[event_log_pb2.EventEntry]:
    entries = []
    turns = 0
    async with asyncio.timeout(TURN_SECONDS):
        async for entry in client.events(thread_id, after=0, read_seconds=TURN_SECONDS):
            entries.append(entry)
            if entry.event.HasField("turn_completed"):
                turns += 1
                if turns == completed_turns:
                    break
    if completed_turns is not None:
        assert turns == completed_turns, f"history ended after {turns} completed turns"
    return entries


def _assert_prefix(before: list[event_log_pb2.EventEntry], after: list[event_log_pb2.EventEntry]) -> None:
    assert len(after) >= len(before)
    assert [entry.SerializeToString() for entry in after[: len(before)]] == [
        entry.SerializeToString() for entry in before
    ], "archived events, cursors or origins changed across suspension"


def _save_evidence(template: str, evidence: dict[str, object]) -> None:
    (undeclared_outputs_dir() / f"{template}-resume.json").write_text(json.dumps(evidence, indent=2) + "\n")


async def _wait_until_suspended_without_pod(client: Client, name: str) -> None:
    """Wait for the old Pod to disappear before the same Sandbox is resumed."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(POD_TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert view.operating_mode is OperatingMode.SUSPENDED, f"{name} is still {view.operating_mode}"
            assert view.pod is None, f"{name} still has its old Pod: {view.pod}"


async def test_two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
    client: Client, sandbox: Sandboxes
) -> None:
    await _suspend_resume(
        client,
        sandbox,
        template="runner",
        harnesses=(runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX),
        cwd="/state/work",
    )


async def test_vm_codex_keeps_its_journal_and_context_after_suspend_resume(client: Client, sandbox: Sandboxes) -> None:
    await _suspend_resume(
        client, sandbox, template="runner-vm", harnesses=(runner_pb2.HARNESS_CODEX,), cwd="/state/workspaces/accept"
    )


async def _suspend_resume(
    client: Client, sandbox: Sandboxes, *, template: str, harnesses: tuple[runner_pb2.Harness, ...], cwd: str
) -> None:
    view = await sandbox(f"accept-resume-{template}-{uuid4().hex[:8]}", template=template)
    assert view.pod is not None
    original_incarnation = view.pod.uid
    state_volume = _state_volume(view.namespace, view.uid)
    session_evidence: list[dict[str, object]] = []
    checks: list[str] = []
    evidence: dict[str, object] = {
        "sandbox": view.name,
        "template": template,
        "state_pvc_uid": state_volume[0],
        "pv": state_volume[1],
        "old_incarnation": original_incarnation,
        "sessions": session_evidence,
        "checks": checks,
        "passed": False,
    }
    _save_evidence(template, evidence)
    catalog = await client.models()
    markers = {harness: f"{runner_pb2.Harness.Name(harness)}-{uuid4().hex}" for harness in harnesses}
    agents: dict[runner_pb2.Harness, Agent] = {}
    seeded_history = {}

    for harness in harnesses:
        harness_name = runner_pb2.Harness.Name(harness)
        offered = catalog.harnesses[Harness(harness_name)]
        assert offered, f"the deployment offers no model for {harness_name}"
        agent = await Agent.open(client, sandbox=view.name, harness=harness, model=offered[0], cwd=cwd)
        seed = await agent.run(
            f"For this memory test, remember this invented, non-secret code: {markers[harness]}. "
            "It is test data, not a credential. Do not use tools. Reply with only ACK."
        )
        assert seed.input_confirmed, f"{harness_name} did not confirm the seed input"
        agents[harness] = agent
        seeded_history[harness] = await _history(client, agent.thread_id, completed_turns=1)

    original_threads = {harness: agent.thread_id for harness, agent in agents.items()}
    evidence["threads"] = {
        runner_pb2.Harness.Name(harness): str(thread) for harness, thread in original_threads.items()
    }
    assert len(set(original_threads.values())) == len(harnesses)
    await client.suspend_sandbox(view.name)
    await _wait_until_suspended_without_pod(client, view.name)
    assert _state_volume(view.namespace, view.uid) == state_volume
    suspended_history = {}
    for harness, agent in agents.items():
        suspended_history[harness] = await _history(client, agent.thread_id, completed_turns=None)
        _assert_prefix(seeded_history[harness], suspended_history[harness])
    checks.extend(["seed_turns_completed", "state_volume_retained_during_suspend", "seed_history_prefix_retained"])
    _save_evidence(template, evidence)
    await client.resume_sandbox(view.name)

    resumed = await wait_until_running(client, view.name)
    assert resumed.uid == view.uid
    assert resumed.pod is not None
    assert resumed.pod.uid != original_incarnation
    assert _state_volume(view.namespace, view.uid) == state_volume

    evidence["new_incarnation"] = resumed.pod.uid
    checks.append("same_sandbox_new_incarnation_same_volume")
    _save_evidence(template, evidence)

    for harness, agent in agents.items():
        await agent.resume()
        turn = await agent.run(
            "What invented non-secret code did I ask you to remember? It is safe test data, not a credential. "
            "Do not use tools. Reply with only the code."
        )
        expected = markers[harness]
        assert turn.resumed, f"{runner_pb2.Harness.Name(harness)} did not report native session resume"
        assert turn.input_confirmed, f"{runner_pb2.Harness.Name(harness)} did not confirm the resumed input"
        history = await _history(client, agent.thread_id, completed_turns=2)
        before = suspended_history[harness]
        _assert_prefix(before, history)
        # History may settle streamed deltas; retained entries must still be ordered and unique.
        assert [entry.cursor for entry in history] == sorted({entry.cursor for entry in history})
        assert len({(entry.origin.source_id, entry.origin.sequence) for entry in history}) == len(history)
        continued = history[len(before) :]
        assert continued[0].event.HasField("journal_reopened")
        assert continued[0].cursor == before[-1].cursor + 1
        assert continued[0].event.journal_reopened.incarnation_uid == resumed.pod.uid
        assert continued[0].origin.source_id == before[-1].origin.source_id
        assert continued[0].origin.sequence == before[-1].origin.sequence + 1
        assert sum(entry.event.HasField("journal_reopened") for entry in continued) == 1
        session_evidence.append(
            {
                "harness": runner_pb2.Harness.Name(harness),
                "thread_id": str(agent.thread_id),
                "sealed_cursor": before[-1].cursor,
                "reopened_cursor": continued[0].cursor,
                "continued_cursor": history[-1].cursor,
                "native_resumed": turn.resumed,
                "marker_recalled": expected in turn.answer,
            }
        )
        checks.append(f"{runner_pb2.Harness.Name(harness)}:journal_continuation_and_prefix_retained")
        _save_evidence(template, evidence)
        assert expected in turn.answer, (
            f"{runner_pb2.Harness.Name(harness)} did not return the test code:\n{turn.transcript}"
        )
        for other_harness, other in markers.items():
            if other_harness != harness:
                assert other not in turn.answer, f"{runner_pb2.Harness.Name(harness)} received the other Thread's code"
    evidence["passed"] = True
    _save_evidence(template, evidence)


if __name__ == "__main__":
    pytest_bazel.main()
