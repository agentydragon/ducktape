"""Completed conversations survive a real Sandbox suspend/resume: the journal continues, and each native
session resumes with its own context."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.env.runtime.harness import Harness
from agentplane.environments.v1 import environment_service_pb2
from agentplane.native.codex.testing import responses_sse
from agentplane.proto import event_log_pb2, event_pb2, identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import (
    TURN_SECONDS,
    WORKING_DIRECTORY,
    Agent,
    Turn,
    environment_runtime_startup_retries,
)
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes, wait_until_running
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
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


def _storage_volumes(cluster: Cluster, sandbox_uid: str) -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        cluster.command("get", "pvc", "-o", "json"), capture_output=True, text=True, check=True, timeout=20
    )
    claims = [
        claim
        for claim in json.loads(result.stdout)["items"]
        if any(
            owner.get("controller") and owner["uid"] == sandbox_uid
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


async def _wait_until_suspended_without_pod(client: Client, target: identity_pb2.EnvironmentTarget) -> None:
    """Wait for the old Pod to disappear before the same Sandbox is resumed."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(POD_TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.get_environment(target)
            assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED, (
                f"{target.name} is still {view.observed_state.operating_mode}"
            )
            assert not view.kubernetes_diagnostics.HasField("pod"), (
                f"{target.name} still has its old Pod: {view.kubernetes_diagnostics.pod}"
            )


async def _wait_until_idle_suspended_without_pod(
    client: Client, target: identity_pb2.EnvironmentTarget
) -> environment_service_pb2.EnvironmentDescriptor:
    """Wait for the controller's idle suspension, distinguishing it from an operator stop."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(POD_TRANSITION_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.get_environment(target)
            assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED, (
                f"{target.name} is still {view.observed_state.operating_mode}"
            )
            assert view.observed_state.suspended_by == "Idle", (
                f"{target.name} stopped for {view.observed_state.suspended_by}, not idleness"
            )
            assert not view.kubernetes_diagnostics.HasField("pod"), (
                f"{target.name} still has its old Pod: {view.kubernetes_diagnostics.pod}"
            )
            return view
    raise AssertionError("unreachable")


async def operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    """Its Identity has no explicit-resume authority; an operator resumes it without replaying work."""
    view = await sandbox(
        f"systest-explicit-resume-{uuid4().hex[:8]}", template=POD_TEMPLATE, idle_suspend_after_seconds=60
    )
    assert view.kubernetes_diagnostics.HasField("pod")
    assert view.identity_name
    assert not view.HasField("execution_account")
    old_pod = view.kubernetes_diagnostics.pod
    target = view.target

    harness = session_pb2.HARNESS_CODEX
    model_name = (await client.models()).harnesses[Harness.CODEX][0]
    agent = await Agent.create(
        client,
        dispatcher,
        sandbox=target.name,
        sandbox_uid=target.uid,
        harness=harness,
        model=model_name,
        cwd=WORKING_DIRECTORY,
    )
    model = scripted.model(old_pod.name, harness)
    # A token is a generation precondition, not authority. Even the operator's current token
    # cannot give this authenticated Identity permission to control the Environment lifecycle.
    assert view.actions.resume_token
    resume_command = shlex.join(
        [
            "/run/agentplane/environment/bin/agentplane",
            "--json",
            "environments",
            "resume",
            target.namespace,
            target.name,
            target.uid,
            "--resume-token",
            view.actions.resume_token,
        ]
    )
    denied_turn = await agent.run_shell(model, resume_command + " || true")
    denied_output = "\n".join(denied_turn.tool_outputs)
    start = denied_output.find("{")
    assert start >= 0, denied_turn.transcript
    denied, _ = json.JSONDecoder().raw_decode(denied_output[start:])
    assert denied["error"]["code"] == "UNAUTHENTICATED", denied_turn.transcript
    failed_prompt = "Reply with exactly MODEL_HTTP_ERROR."
    failed_task = asyncio.create_task(agent.run(failed_prompt))
    for _ in range(3):
        request = await model.request()
        assert request.user_texts[-1] == failed_prompt
        await model.http_error(request)
    try:
        await failed_task
    except AssertionError as error:
        if "TURN_STATUS_FAILED" not in str(error):
            raise AssertionError(f"unexpected failure for the original command: {error}") from error
    else:
        raise AssertionError("the scripted upstream errors did not fail the original command")

    failed_history = await _history(client, agent.thread_id, completed_turns=2)
    failed_entry = [entry for entry in failed_history if entry.event.HasField("command_admitted")][-1]
    failed_command_id = failed_entry.event.command_admitted.command.command_id
    (failed_turn_entry,) = [
        entry
        for entry in failed_history
        if entry.event.HasField("turn_completed") and entry.event.turn_completed.status == event_pb2.TURN_STATUS_FAILED
    ]

    async with asyncio.timeout(POD_TRANSITION_SECONDS + 120):
        suspended = await _wait_until_idle_suspended_without_pod(client, target)
    assert suspended.target.uid == target.uid

    await client.resume_sandbox(target)

    resumed = await wait_until_running(client, target)
    assert resumed.kubernetes_diagnostics.HasField("pod")
    assert resumed.kubernetes_diagnostics.pod.uid != old_pod.uid
    assert resumed.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING

    resumed_model = scripted.model(resumed.kubernetes_diagnostics.pod.name, harness)
    fresh_turn = await agent.run_shell(resumed_model, "printf 'EXPLICIT_RESUME_EXEC_OK\\n'")
    assert fresh_turn.input_confirmed
    assert fresh_turn.resumed
    assert any("EXPLICIT_RESUME_EXEC_OK" in output for output in fresh_turn.tool_outputs), fresh_turn.transcript

    resumed_history = await _history(client, agent.thread_id, completed_turns=3)
    _assert_prefix(failed_history, resumed_history)
    admitted = [
        entry.event.command_admitted.command.command_id
        for entry in resumed_history
        if entry.event.HasField("command_admitted")
    ]
    assert len(admitted) == 3, f"expected the denied lifecycle probe, failed command and fresh command, got {admitted}"
    assert admitted[1] == failed_command_id
    fresh_turn_command_id = admitted[2]
    assert fresh_turn_command_id != failed_command_id
    (retained_failed_turn,) = [
        entry
        for entry in resumed_history
        if entry.event.HasField("turn_completed") and entry.event.turn_completed.status == event_pb2.TURN_STATUS_FAILED
    ]
    assert retained_failed_turn.SerializeToString() == failed_turn_entry.SerializeToString()

    (undeclared_outputs_dir() / "explicit-resume-idle-pod.json").write_text(
        json.dumps(
            {
                "sandbox": MessageToDict(target, preserving_proto_field_name=True),
                "identity_name": view.identity_name,
                "identity_resume_denial": denied["error"],
                "old_pod_uid": old_pod.uid,
                "new_pod_uid": resumed.kubernetes_diagnostics.pod.uid,
                "resumed_by": "operator",
                "failed_command_id": failed_command_id,
                "failed_turn_id": failed_turn_entry.event.turn_completed.turn_id,
                "fresh_command_id": fresh_turn_command_id,
                "fresh_exec_output": [
                    output for output in fresh_turn.tool_outputs if "EXPLICIT_RESUME_EXEC_OK" in output
                ],
            },
            indent=2,
        )
        + "\n"
    )


@dataclass
class _ResumeRun:
    """One Sandbox's Threads through a suspend/resume, checked and recorded at each step.

    Each test plays the model itself.
    """

    cluster: Cluster
    client: Client
    dispatcher: SessionDispatcherClient
    view: environment_service_pb2.EnvironmentDescriptor
    template: str
    original_incarnation: str
    storage_volumes: dict[str, tuple[str, str]]
    agents: dict[session_pb2.Harness, Agent] = field(default_factory=dict)
    seeded_history: dict[session_pb2.Harness, list[event_log_pb2.EventEntry]] = field(default_factory=dict)
    suspended_history: dict[session_pb2.Harness, list[event_log_pb2.EventEntry]] = field(default_factory=dict)
    native_session_ids: dict[str, str] = field(default_factory=dict)
    initial_boot_id: str | None = None
    resumed_incarnation: str | None = None
    sessions: list[dict[str, object]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    passed: bool = False

    @classmethod
    async def start(
        cls, cluster: Cluster, client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes, *, template: str
    ) -> _ResumeRun:
        view = await sandbox(f"systest-resume-{template}-{uuid4().hex[:8]}", template=template)
        assert view.kubernetes_diagnostics.HasField("pod")
        run = cls(
            cluster=cluster,
            client=client,
            dispatcher=dispatcher,
            view=view,
            template=template,
            original_incarnation=view.kubernetes_diagnostics.pod.uid,
            storage_volumes=_storage_volumes(cluster, view.target.uid),
        )
        run.save()
        return run

    @property
    def pod_name(self) -> str:
        """The Sandbox's current Pod: a Pod Sandbox's every incarnation keeps its name, a VM's launcher does not."""
        assert self.view.kubernetes_diagnostics.HasField("pod")
        return self.view.kubernetes_diagnostics.pod.name

    async def create(self, harness: session_pb2.Harness, *, cwd: str, instructions: str = "") -> Agent:
        """A Thread on the first model the deployment offers `harness`, with no native process yet."""
        harness_name = session_pb2.Harness.Name(harness)
        offered = (await self.client.models()).harnesses[Harness(harness_name)]
        assert offered, f"the deployment offers no model for {harness_name}"
        agent = await Agent.create(
            self.client,
            self.dispatcher,
            sandbox=self.view.target.name,
            sandbox_uid=self.view.target.uid,
            harness=harness,
            model=offered[0],
            cwd=cwd,
            instructions=instructions,
        )
        snapshot = await self.client.runner_status(self.view.target.name, self.view.target.uid)
        self.initial_boot_id = snapshot.environment_runtime_boot_id
        reserved = next(row for row in snapshot.sessions if row.session_id == str(agent.thread_id))
        assert not reserved.HasField("process_id"), "CreateSession unexpectedly started a native harness"
        self.agents[harness] = agent
        return agent

    async def seeded(self, harness: session_pb2.Harness, seed: Turn) -> None:
        """Check the seed turn and the native session it started."""
        assert seed.input_confirmed, f"{session_pb2.Harness.Name(harness)} did not confirm the seed input"
        agent = self.agents[harness]
        self.seeded_history[harness] = await _history(self.client, agent.thread_id, completed_turns=1)
        snapshot = await self.client.runner_status(self.view.target.name, self.view.target.uid)
        ready = next(row for row in snapshot.sessions if row.session_id == str(agent.thread_id))
        assert ready.execution_state == session_pb2.SESSION_EXECUTION_STATE_READY
        assert ready.HasField("process_id")
        assert ready.HasField("native_session_id")
        assert any(
            process.process_id == ready.process_id and ready.session_id in process.assigned_session_ids
            for process in snapshot.processes
        )
        self.native_session_ids[ready.session_id] = ready.native_session_id

    async def suspend_and_resume(self) -> None:
        """Suspend until the old Pod is gone, resume, and check what the new runner holds."""
        with tracer.start_as_current_span(
            "system_test.sandbox.suspend_resume",
            attributes={"sandbox.name": self.view.target.name, "sandbox.uid": str(self.view.target.uid)},
        ):
            assert len({agent.thread_id for agent in self.agents.values()}) == len(self.agents)
            await self.client.suspend_sandbox(self.view.target)
            await _wait_until_suspended_without_pod(self.client, self.view.target)
            assert _storage_volumes(self.cluster, self.view.target.uid) == self.storage_volumes
            for harness, agent in self.agents.items():
                self.suspended_history[harness] = await _history(self.client, agent.thread_id, completed_turns=None)
                _assert_prefix(self.seeded_history[harness], self.suspended_history[harness])
                # Dispatcher returns its durable receipt without contacting the suspended runner.
                (seed,) = [entry for entry in self.seeded_history[harness] if entry.event.HasField("command_admitted")]
                command = seed.event.command_admitted.command
                receipt = await agent.submit_input(command.command_id, command.submit_input.text)
                assert receipt.command_id == command.command_id
                assert receipt.admission == seed
            self.checks.extend(
                [
                    "seed_turns_completed",
                    "storage_volumes_retained_during_suspend",
                    "seed_history_prefix_retained",
                    "seed_input_retry_answered_by_dispatcher",
                ]
            )
            self.save()
            await self.client.resume_sandbox(self.view.target)

            resumed = await wait_until_running(self.client, self.view.target)
            assert resumed.target.uid == self.view.target.uid
            assert resumed.kubernetes_diagnostics.HasField("pod")
            assert resumed.kubernetes_diagnostics.pod.uid != self.original_incarnation
            assert _storage_volumes(self.cluster, self.view.target.uid) == self.storage_volumes
            self.view = resumed
            self.resumed_incarnation = resumed.kubernetes_diagnostics.pod.uid
            self.checks.append("same_sandbox_new_incarnation_same_volumes")
            async for attempt in environment_runtime_startup_retries():
                with attempt:
                    snapshot = await self.client.runner_status(self.view.target.name, self.view.target.uid)
            assert snapshot.environment_runtime_boot_id != self.initial_boot_id
            assert {row.session_id for row in snapshot.sessions} == set(self.native_session_ids)
            for row in snapshot.sessions:
                if row.session_id in self.native_session_ids:
                    assert row.execution_state == session_pb2.SESSION_EXECUTION_STATE_DORMANT
                    assert not row.HasField("process_id")
                    assert row.native_session_id == self.native_session_ids[row.session_id]
            self.checks.append("new_runner_retains_dormant_sessions_without_native_processes")
            self.save()

    async def continued(self, harness: session_pb2.Harness, turn: Turn) -> None:
        """Check the first turn after the resume: the native session resumed and the journal continued."""
        harness_name = session_pb2.Harness.Name(harness)
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
            "sandbox": self.view.target.name,
            "template": self.template,
            "storage_volumes": self.storage_volumes,
            "old_incarnation": self.original_incarnation,
            "new_incarnation": self.resumed_incarnation,
            "threads": {
                session_pb2.Harness.Name(harness): str(agent.thread_id) for harness, agent in self.agents.items()
            },
            "sessions": self.sessions,
            "checks": self.checks,
            "passed": self.passed,
        }
        (undeclared_outputs_dir() / f"{self.template}-resume.json").write_text(json.dumps(evidence, indent=2) + "\n")


def _marker(harness: session_pb2.Harness) -> str:
    return f"{session_pb2.Harness.Name(harness)}-{uuid4().hex}"


async def two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    executor: Executor,
) -> None:
    """One replacement of the Sandbox's Pod, or of a VM's launcher, exercises both native stores."""
    template, cwd = executor.template, executor.cwd
    run = await _ResumeRun.start(cluster, client, dispatcher, sandbox, template=template)
    markers = {harness: _marker(harness) for harness in (session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX)}
    models = {harness: scripted.model(run.pod_name, harness) for harness in markers}
    for harness, marker in markers.items():
        agent = await run.create(harness, cwd=cwd)
        async with asyncio.TaskGroup() as turns:
            seed = turns.create_task(agent.run(SEED_PROMPT.format(marker=marker)))
            await models[harness].reply(await models[harness].request(), Text("ACK"))
        await run.seeded(harness, seed.result())

    await run.suspend_and_resume()

    # A VM's new launcher Pod has a name of its own.
    models = {harness: scripted.model(run.pod_name, harness) for harness in markers}
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


async def codex_standing_instructions_after_suspend_and_compact(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    standing = "SESSION_STANDING_ORDER: consult Agentplane before declaring access unavailable."
    run = await _ResumeRun.start(cluster, client, dispatcher, sandbox, template=POD_TEMPLATE)
    agent = await run.create(session_pb2.HARNESS_CODEX, cwd=WORKING_DIRECTORY, instructions=standing)
    model = scripted.model(run.pod_name, session_pb2.HARNESS_CODEX)
    assert isinstance(model, CodexModel)
    async with asyncio.TaskGroup() as turns:
        seed = turns.create_task(agent.run("Remember SEEDED_CONTEXT_MARKER and reply ACK."))
        seed_request = await model.request()
        assert standing in seed_request.system_text
        assert "# Agentplane environment" in seed_request.system_text
        await model.reply(seed_request, Text("ACK"))
    await run.seeded(session_pb2.HARNESS_CODEX, seed.result())
    await run.suspend_and_resume()

    model = scripted.model(run.pod_name, session_pb2.HARNESS_CODEX)
    assert isinstance(model, CodexModel)
    async with asyncio.TaskGroup() as turns:
        recall = turns.create_task(agent.run(RECALL_PROMPT))
        async with await model.endpoint.await_next_request() as exchange:
            developer = "\n".join(message.text for message in exchange.request.messages("developer"))
            assert standing in developer
            assert "# Agentplane environment" in developer
            await exchange.send(
                *responses_sse.response_stream(
                    [responses_sse.Message("SEEDED_CONTEXT_MARKER")],
                    model=exchange.request.model,
                    usage={"input_tokens": 1_000_000, "output_tokens": 5, "total_tokens": 1_000_005},
                ).events
            )
    await run.continued(session_pb2.HARNESS_CODEX, recall.result())

    async with asyncio.TaskGroup() as turns:
        turns.create_task(agent.run("Continue after checkpoint and reply CONTINUED_OK."))
        async with await model.endpoint.await_next_request() as exchange:
            await exchange.send(
                *responses_sse.response_stream(
                    [responses_sse.Message("SUMMARY_CHECKPOINT_MARKER")], model=exchange.request.model
                ).events
            )
        async with await model.endpoint.await_next_request() as exchange:
            request = exchange.request
            assert any("SUMMARY_CHECKPOINT_MARKER" in message.text for message in request.messages("user"))
            developer = "\n".join(message.text for message in request.messages("developer"))
            assert "# Agentplane environment" in developer
            assert standing in developer
            await exchange.send(
                *responses_sse.response_stream([responses_sse.Message("CONTINUED_OK")], model=request.model).events
            )
    run.checks.append("standing_instructions_retained_after_suspend_resume_and_auto_compaction")
    run.passed = True
    run.save()
