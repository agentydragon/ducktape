"""Completed conversations survive a real Sandbox suspend/resume: the journal continues, and each native
session resumes with its own context."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.agent import TURN_SECONDS, WORKING_DIRECTORY, Agent, Turn, runner_startup_retries
from agentplane.acceptance.conftest import POD_TEMPLATE, Sandboxes, wait_until_running
from agentplane.acceptance.scripted_upstream import ScriptedUpstream
from agentplane.app.sandbox_models import SandboxView
from agentplane.app.testing.client import Client
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import OperatingMode
from agentplane.testing.scripted_model import Text
from util.testing.undeclared_outputs import undeclared_outputs_dir

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

POD_TRANSITION_SECONDS = 300.0

SEED_PROMPT = (
    "For this memory test, remember this invented, non-secret code: {marker}. "
    "It is test data, not a credential. Do not use tools. Reply with only ACK."
)
RECALL_PROMPT = (
    "What invented non-secret code did I ask you to remember? It is safe test data, not a credential. "
    "Do not use tools. Reply with only the code."
)


def _storage_volumes(namespace: str, sandbox_uid: UUID) -> dict[str, tuple[str, str]]:
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
    assert claims, "expected Sandbox-owned storage PVCs"
    for claim in claims:
        assert claim["status"]["phase"] == "Bound"
    return {claim["metadata"]["name"]: (claim["metadata"]["uid"], claim["spec"]["volumeName"]) for claim in claims}


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


@dataclass
class _ResumeRun:
    """One Sandbox's Threads through a suspend/resume, checked and recorded at each step.

    The model turns stay with each test: a real model answers them, or the test answers them itself.
    """

    client: Client
    view: SandboxView
    template: str
    original_incarnation: str
    storage_volumes: dict[str, tuple[str, str]]
    agents: dict[runner_pb2.Harness, Agent] = field(default_factory=dict)
    seeded_history: dict[runner_pb2.Harness, list[event_log_pb2.EventEntry]] = field(default_factory=dict)
    suspended_history: dict[runner_pb2.Harness, list[event_log_pb2.EventEntry]] = field(default_factory=dict)
    native_session_ids: dict[str, str] = field(default_factory=dict)
    initial_boot_id: str | None = None
    resumed_incarnation: str | None = None
    sessions: list[dict[str, object]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    passed: bool = False

    @classmethod
    async def start(cls, client: Client, sandbox: Sandboxes, *, template: str) -> _ResumeRun:
        view = await sandbox(f"accept-resume-{template}-{uuid4().hex[:8]}", template=template)
        assert view.pod is not None
        run = cls(
            client=client,
            view=view,
            template=template,
            original_incarnation=view.pod.uid,
            storage_volumes=_storage_volumes(view.namespace, view.uid),
        )
        run.save()
        return run

    @property
    def pod_name(self) -> str:
        """The Sandbox's Pod, by the name every incarnation keeps."""
        assert self.view.pod is not None
        return self.view.pod.name

    async def open(self, harness: runner_pb2.Harness, *, cwd: str) -> Agent:
        """A Thread on the first model the deployment offers `harness`, with no native process yet."""
        harness_name = runner_pb2.Harness.Name(harness)
        offered = (await self.client.models()).harnesses[Harness(harness_name)]
        assert offered, f"the deployment offers no model for {harness_name}"
        agent = await Agent.open(self.client, sandbox=self.view.name, harness=harness, model=offered[0], cwd=cwd)
        snapshot = await self.client.runner_status(self.view.name)
        self.initial_boot_id = snapshot.runner_boot_id
        reserved = next(row for row in snapshot.sessions if row.session_id == str(agent.thread_id))
        assert reserved.process_id is None, "OpenSession unexpectedly started a native harness"
        self.agents[harness] = agent
        return agent

    async def seeded(self, harness: runner_pb2.Harness, seed: Turn) -> None:
        """Check the seed turn and the native session it started."""
        assert seed.input_confirmed, f"{runner_pb2.Harness.Name(harness)} did not confirm the seed input"
        agent = self.agents[harness]
        self.seeded_history[harness] = await _history(self.client, agent.thread_id, completed_turns=1)
        snapshot = await self.client.runner_status(self.view.name)
        ready = next(row for row in snapshot.sessions if row.session_id == str(agent.thread_id))
        assert ready.execution_state == "SESSION_EXECUTION_STATE_READY"
        assert ready.process_id is not None
        assert ready.native_session_id is not None
        assert any(
            process.process_id == ready.process_id and ready.session_id in process.assigned_session_ids
            for process in snapshot.processes
        )
        self.native_session_ids[ready.session_id] = ready.native_session_id

    async def suspend_and_resume(self) -> None:
        """Suspend until the old Pod is gone, resume, and check what the new runner holds."""
        assert len({agent.thread_id for agent in self.agents.values()}) == len(self.agents)
        await self.client.suspend_sandbox(self.view.name)
        await _wait_until_suspended_without_pod(self.client, self.view.name)
        assert _storage_volumes(self.view.namespace, self.view.uid) == self.storage_volumes
        for harness, agent in self.agents.items():
            self.suspended_history[harness] = await _history(self.client, agent.thread_id, completed_turns=None)
            _assert_prefix(self.seeded_history[harness], self.suspended_history[harness])
        self.checks.extend(
            ["seed_turns_completed", "storage_volumes_retained_during_suspend", "seed_history_prefix_retained"]
        )
        self.save()
        await self.client.resume_sandbox(self.view.name)

        resumed = await wait_until_running(self.client, self.view.name)
        assert resumed.uid == self.view.uid
        assert resumed.pod is not None
        assert resumed.pod.uid != self.original_incarnation
        assert _storage_volumes(self.view.namespace, self.view.uid) == self.storage_volumes
        self.resumed_incarnation = resumed.pod.uid
        self.checks.append("same_sandbox_new_incarnation_same_volumes")
        async for attempt in runner_startup_retries():
            with attempt:
                snapshot = await self.client.runner_status(self.view.name)
        assert snapshot.runner_boot_id != self.initial_boot_id
        assert {row.session_id for row in snapshot.sessions} == set(self.native_session_ids)
        for row in snapshot.sessions:
            if row.session_id in self.native_session_ids:
                assert row.execution_state == "SESSION_EXECUTION_STATE_DORMANT"
                assert row.process_id is None
                assert row.native_session_id == self.native_session_ids[row.session_id]
        self.checks.append("new_runner_retains_dormant_sessions_without_native_processes")
        self.save()

    async def continued(self, harness: runner_pb2.Harness, turn: Turn) -> None:
        """Check the first turn after the resume: the native session resumed and the journal continued."""
        harness_name = runner_pb2.Harness.Name(harness)
        assert turn.resumed, f"{harness_name} did not report native session resume"
        assert turn.input_confirmed, f"{harness_name} did not confirm the resumed input"
        agent = self.agents[harness]
        history = await _history(self.client, agent.thread_id, completed_turns=2)
        before = self.suspended_history[harness]
        _assert_prefix(before, history)
        # History may settle streamed deltas; retained entries must still be ordered and unique.
        assert [entry.cursor for entry in history] == sorted({entry.cursor for entry in history})
        assert len({(entry.origin.source_id, entry.origin.sequence) for entry in history}) == len(history)
        continued = history[len(before) :]
        assert continued[0].event.HasField("journal_reopened")
        assert continued[0].cursor == before[-1].cursor + 1
        assert continued[0].event.journal_reopened.incarnation_uid == self.resumed_incarnation
        assert continued[0].origin.source_id == before[-1].origin.source_id
        assert continued[0].origin.sequence == before[-1].origin.sequence + 1
        assert sum(entry.event.HasField("journal_reopened") for entry in continued) == 1
        self.sessions.append(
            {
                "harness": harness_name,
                "thread_id": str(agent.thread_id),
                "sealed_cursor": before[-1].cursor,
                "reopened_cursor": continued[0].cursor,
                "continued_cursor": history[-1].cursor,
                "native_resumed": turn.resumed,
            }
        )
        self.checks.append(f"{harness_name}:journal_continuation_and_prefix_retained")
        self.save()

    def save(self) -> None:
        evidence = {
            "sandbox": self.view.name,
            "template": self.template,
            "storage_volumes": self.storage_volumes,
            "old_incarnation": self.original_incarnation,
            "new_incarnation": self.resumed_incarnation,
            "threads": {
                runner_pb2.Harness.Name(harness): str(agent.thread_id) for harness, agent in self.agents.items()
            },
            "sessions": self.sessions,
            "checks": self.checks,
            "passed": self.passed,
        }
        (undeclared_outputs_dir() / f"{self.template}-resume.json").write_text(json.dumps(evidence, indent=2) + "\n")


def _marker(harness: runner_pb2.Harness) -> str:
    return f"{runner_pb2.Harness.Name(harness)}-{uuid4().hex}"


@pytest.mark.scripted_model
async def test_two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
    client: Client, sandbox: Sandboxes, scripted: ScriptedUpstream
) -> None:
    """One Pod replacement exercises both native stores."""
    run = await _ResumeRun.start(client, sandbox, template=POD_TEMPLATE)
    markers = {harness: _marker(harness) for harness in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX)}
    models = {harness: scripted.model(run.pod_name, harness) for harness in markers}
    for harness, marker in markers.items():
        agent = await run.open(harness, cwd=WORKING_DIRECTORY)
        async with asyncio.TaskGroup() as turns:
            seed = turns.create_task(agent.run(SEED_PROMPT.format(marker=marker)))
            await models[harness].reply(await models[harness].request(), Text("ACK"))
        await run.seeded(harness, seed.result())

    await run.suspend_and_resume()

    for harness, agent in run.agents.items():
        async with asyncio.TaskGroup() as turns:
            recall = turns.create_task(agent.run(RECALL_PROMPT))
            request = await models[harness].request()
            # The resumed native session sends the model its own earlier turns, and only its own.
            context = "\n".join(request.user_texts)
            assert markers[harness] in context
            assert not [other for other in markers.values() if other != markers[harness] and other in context]
            await models[harness].reply(request, Text(markers[harness]))
        await run.continued(harness, recall.result())
    run.passed = True
    run.save()


@pytest.mark.real_model
@pytest.mark.vm_sandboxes
async def test_vm_codex_keeps_its_journal_and_context_after_suspend_resume(client: Client, sandbox: Sandboxes) -> None:
    run = await _ResumeRun.start(client, sandbox, template="runner-vm")
    marker = _marker(runner_pb2.HARNESS_CODEX)
    agent = await run.open(runner_pb2.HARNESS_CODEX, cwd="/state/workspaces/accept")
    await run.seeded(runner_pb2.HARNESS_CODEX, await agent.run(SEED_PROMPT.format(marker=marker)))

    await run.suspend_and_resume()

    turn = await agent.run(RECALL_PROMPT)
    await run.continued(runner_pb2.HARNESS_CODEX, turn)
    assert marker in turn.answer, f"Codex did not return the test code:\n{turn.transcript}"
    run.passed = True
    run.save()


if __name__ == "__main__":
    pytest_bazel.main()
