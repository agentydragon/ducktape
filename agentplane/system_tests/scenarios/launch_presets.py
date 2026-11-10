"""A launch preset, from the preset list to a resumed Sandbox. The k3s target configures the preset
(`launch_preset.py`)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.env.runtime.harness import Harness
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import environment_lifecycle_pb2, session_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import launch_preset
from agentplane.system_tests.driver.agent import RUNNER_ANSWERS_SECONDS, Agent
from agentplane.system_tests.driver.sandbox import SANDBOX_READY_SECONDS, Sandboxes, wait_until_running
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.scripted_model import ShellCall, Text

INSTRUCTIONS = "Test instructions that override the preset's for this session."


def _polling(seconds: float) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_delay(seconds),
        wait=wait_fixed(0.2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    )


async def a_preset_launches_an_initialized_editable_codex_thread(
    client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes, scripted: ScriptedUpstream
) -> None:
    with tracer.start_as_current_span(
        "system_test.launch_preset.resolve", record_exception=False, set_status_on_exception=False
    ):
        configured = {preset.name: preset for preset in (await client.presets()).presets}
        preset = configured[launch_preset.SANDBOX_PRESET]
    assert preset.session_defaults.harness == session_pb2.HARNESS_CODEX

    view = await sandbox(
        "systest-test-preset",
        template=preset.template,
        preset=launch_preset.SANDBOX_PRESET,
        policies=list(preset.policy_sets),
        bootstrap=preset.bootstrap,
    )
    assert view.configuration.preset == launch_preset.SANDBOX_PRESET
    assert view.configuration.HasField("binding")

    with tracer.start_as_current_span(
        "system_test.launch_preset.admit_first_session", record_exception=False, set_status_on_exception=False
    ):
        first_id = uuid4()
        session = await client.create_session(
            view.target.name, first_id, sandbox_uid=view.target.uid, overrides={"instructions": INSTRUCTIONS}
        )
        assert session.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_WAITING
        retried = await client.create_session(
            view.target.name, first_id, sandbox_uid=view.target.uid, overrides={"instructions": INSTRUCTIONS}
        )
        assert (retried.session_id, retried.placement.environment_hosted.environment.name, retried.created_at) == (
            session.session_id,
            session.placement.environment_hosted.environment.name,
            session.created_at,
        )
        await client.wait_for_session_acceptance(
            dispatcher, first_id, session=session, timeout_s=RUNNER_ANSWERS_SECONDS
        )
        first = await client.wait_for_thread_feed(first_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    expected_reasoning_effort = (
        preset.session_defaults.reasoning_effort if preset.session_defaults.HasField("reasoning_effort") else None
    )
    assert (first.harness, first.model, first.reasoning_effort) == (
        Harness.CODEX,
        preset.session_defaults.model,
        expected_reasoning_effort,
    )

    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, session_pb2.HARNESS_CODEX)
    agent = Agent(
        client,
        dispatcher,
        thread_id=first.id,
        cursor=first.last_cursor,
        sandbox=session.placement.environment_hosted.environment.name,
        sandbox_uid=session.placement.environment_hosted.environment.uid,
    )
    with tracer.start_as_current_span(
        "system_test.launch_preset.initialized_turn", record_exception=False, set_status_on_exception=False
    ):
        async with asyncio.TaskGroup() as turns:
            turn = turns.create_task(agent.run("Report what the workspace's ready marker says."))
            request = await model.request()
            # The preset's model and the session's own instructions reach the model.
            assert request.model == preset.session_defaults.model
            assert INSTRUCTIONS in request.system_text, request.system_text
            await model.reply(
                request, ShellCall(call_id="read-ready-marker", command=f"cat {launch_preset.READY_MARKER}")
            )
            request = await model.request()
            # The bootstrap ran in the Sandbox before the session's first command.
            assert any(output.contains(launch_preset.READY_TEXT) for output in request.tool_outputs), (
                request.tool_outputs
            )
            await model.reply(request, Text("The workspace is initialized."))
        assert launch_preset.READY_TEXT in turn.result().transcript

    previous_incarnation = view.kubernetes_diagnostics.pod.uid
    with tracer.start_as_current_span(
        "system_test.launch_preset.suspend_queue", record_exception=False, set_status_on_exception=False
    ):
        await client.suspend_sandbox(view.target)
        async for attempt in _polling(SANDBOX_READY_SECONDS):
            with attempt:
                suspended = await client.get_environment(view.target)
                assert suspended.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
                assert not suspended.kubernetes_diagnostics.HasField("pod"), (
                    f"Sandbox {view.target.name} still has a Pod"
                )

        suspended_id = uuid4()
        suspended_session = await client.create_session(view.target.name, suspended_id, sandbox_uid=view.target.uid)
        assert (
            suspended_session.status.materialization
            == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_WAITING
        )
        assert not suspended_session.status.HasField("delivery_blocker")
        assert suspended_session.status.wake_assessment.state == environment_lifecycle_pb2.WAKE_STATE_NOT_EVALUATED
        async for attempt in _polling(RUNNER_ANSWERS_SECONDS):
            with attempt:
                suspended_session = await dispatcher.get_session(str(suspended_id))
                if (
                    suspended_session.status.materialization
                    != session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_WAITING
                ):
                    raise RuntimeError(f"Suspended Session {suspended_id} left WAITING: {suspended_session.status}")
                status = suspended_session.status
                assert status.HasField("delivery_blocker"), f"Suspended Session {suspended_id}: {status}"
                assert (
                    status.delivery_blocker.kind
                    == environment_lifecycle_pb2.DELIVERY_BLOCKER_KIND_OPERATOR_REQUESTED_STOP
                )
                assert status.wake_assessment.state == environment_lifecycle_pb2.WAKE_STATE_DENIED
                assert status.wake_assessment.reason == environment_lifecycle_pb2.WAKE_REASON_OPERATOR_REQUESTED_STOP
        still_suspended = await client.get_environment(view.target)
        assert still_suspended.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
        assert not still_suspended.kubernetes_diagnostics.HasField("pod")

    with tracer.start_as_current_span(
        "system_test.launch_preset.resume_and_accept", record_exception=False, set_status_on_exception=False
    ):
        await client.resume_sandbox(view.target)
        resumed = await wait_until_running(client, view.target)
        assert resumed.target.uid == view.target.uid
        assert resumed.kubernetes_diagnostics.HasField("pod")
        assert resumed.kubernetes_diagnostics.pod.uid != previous_incarnation
        await client.wait_for_session_acceptance(
            dispatcher, suspended_id, session=suspended_session, timeout_s=RUNNER_ANSWERS_SECONDS
        )
        await client.wait_for_thread_projection(suspended_id, timeout_s=RUNNER_ANSWERS_SECONDS)

    with tracer.start_as_current_span(
        "system_test.launch_preset.session_defaults", record_exception=False, set_status_on_exception=False
    ):
        inherited_id = uuid4()
        await client.create_session_and_wait(
            view.target.name, inherited_id, dispatcher=dispatcher, sandbox_uid=view.target.uid
        )
        inherited = await client.wait_for_thread_feed(inherited_id, timeout_s=RUNNER_ANSWERS_SECONDS)
        local_model = "systest-local-model-override"
        local_id = uuid4()
        await client.create_session_and_wait(
            view.target.name,
            local_id,
            dispatcher=dispatcher,
            sandbox_uid=view.target.uid,
            overrides={"model": local_model},
        )
        local = await client.wait_for_thread_feed(local_id, timeout_s=RUNNER_ANSWERS_SECONDS)
        assert inherited.model == preset.session_defaults.model
        assert local.model == local_model


# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
