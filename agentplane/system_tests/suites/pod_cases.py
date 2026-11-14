"""Pod scenario bindings with fresh Sessions and explicit runner ownership."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.harness_pair import run_harness_pair, run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import (
    agent_messaging,
    dispatcher_workload,
    egress,
    launch_presets,
    runner_credentials,
    session_instructions,
    storage,
    suspend_resume,
    thread_history,
)
from agentplane.system_tests.suites import pod_runners


@dataclass(frozen=True)
class Resources:
    cluster: Cluster
    base_url: str
    token: str
    scripted: ScriptedUpstream
    dispatcher: SessionDispatcherClient
    borrow: pod_runners.Borrow

    @asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[Client, Sandboxes]]:
        test_tmpdir = Path(os.environ["TEST_TMPDIR"])
        if not test_tmpdir.is_absolute():
            raise RuntimeError("TEST_TMPDIR must be absolute for the native Gateway token")
        with TemporaryDirectory(prefix="agentplane-gateway-", dir=test_tmpdir) as directory:
            async with (
                self.scripted.case_scope(),
                scenario.open_resources(
                    self.cluster,
                    self.base_url,
                    self.token,
                    tracer,
                    token_file=Path(directory) / "gateway-token",
                    native_diagnostics=False,
                ) as owned,
            ):
                yield owned

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[tuple[Client, pod_runners.Pair]]:
        # Settle the model queues before returning this pair to another case.
        async with (
            self.borrow() as runners,
            self.scripted.case_scope(),
            Client.connect(base_url=self.base_url, token=self.token) as client,
        ):
            yield client, runners


type Scenario = Callable[[Resources], Awaitable[None]]


async def an_agent_messages_another_which_reads_and_acknowledges_it(resources: Resources) -> None:
    async with resources.open() as (client, _):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, model, sandboxes: agent_messaging.an_agent_messages_another_which_reads_and_acknowledges_it(
                resources.cluster, client, resources.dispatcher, sandboxes, harness, model, resources.scripted
            ),
        )


async def a_policy_granted_after_the_sandbox_is_running_takes_effect(resources: Resources) -> None:
    async with resources.open() as (client, _):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, model, sandboxes: egress.a_policy_granted_after_the_sandbox_is_running_takes_effect(
                resources.cluster, client, resources.dispatcher, sandboxes, harness, model, resources.scripted
            ),
        )


async def the_model_call_itself_goes_through_the_proxy(resources: Resources) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: egress.the_model_call_itself_goes_through_the_proxy(
                resources.cluster,
                client,
                resources.dispatcher,
                runners.for_harness(harness),
                harness,
                model,
                resources.scripted,
            ),
        )


async def sandbox_cli_reaches_the_dispatcher_with_its_workload_svid(resources: Resources) -> None:
    async with resources.open() as (client, _):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, model, sandboxes: (
                dispatcher_workload.sandbox_cli_denies_create_but_lists_its_identity_session(
                    client, resources.dispatcher, sandboxes, harness, model, resources.scripted
                )
            ),
        )


async def a_preset_launches_an_initialized_editable_codex_thread(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await launch_presets.a_preset_launches_an_initialized_editable_codex_thread(
            client, resources.dispatcher, sandboxes, resources.scripted
        )


async def pod_mtls_session_survives_replacement_and_keeps_history(resources: Resources) -> None:
    async with resources.open() as (client, _):
        async with asyncio.timeout(600):
            await run_harness_pair_in_own_sandboxes(
                resources.cluster,
                client,
                lambda harness, model, sandboxes: (
                    runner_credentials.pod_mtls_session_survives_replacement_and_keeps_history(
                        resources.cluster, client, resources.dispatcher, sandboxes, harness, model, resources.scripted
                    )
                ),
            )


async def the_standing_instruction_reaches_the_model_and_only_in_its_own_session(resources: Resources) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: (
                session_instructions.the_standing_instruction_reaches_the_model_and_only_in_its_own_session(
                    client, resources.dispatcher, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


async def fresh_workspace_storage_survives_replacement(resources: Resources) -> None:
    async with resources.open() as (client, _sandboxes):
        await storage.fresh_workspace_storage_survives_replacement(
            resources.cluster,
            client,
            resources.dispatcher,
            Executor(template=POD_TEMPLATE, cwd="/state/work"),
            vm_storage=None,
        )


async def codex_standing_instructions_after_suspend_and_compact(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.codex_standing_instructions_after_suspend_and_compact(
            resources.cluster, client, resources.dispatcher, sandboxes, resources.scripted
        )


async def two_harness_threads_keep_their_context_after_sandbox_suspend_resume(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
            resources.cluster,
            client,
            resources.dispatcher,
            sandboxes,
            resources.scripted,
            Executor(template=POD_TEMPLATE, cwd="/state/work"),
        )


async def operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control(
            resources.cluster, client, resources.dispatcher, sandboxes, resources.scripted
        )


async def an_identity_creates_a_native_session_only_with_an_explicit_binding_grant(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.an_identity_creates_a_native_session_only_with_an_explicit_binding_grant(
            resources.cluster, client, resources.dispatcher, sandboxes, resources.scripted
        )


async def an_unknown_history_session_is_not_found(resources: Resources) -> None:
    async with Client.connect(base_url=resources.base_url, token=resources.token) as client:
        await thread_history.an_unknown_history_session_is_not_found(client)


async def every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor(resources: Resources) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: (
                thread_history.every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor(
                    client, resources.dispatcher, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


async def turns_nobody_watches_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn(
    resources: Resources,
) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: (
                thread_history.unwatched_turns_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn(
                    client, resources.dispatcher, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


SCENARIOS = (
    # CaseBatch runs storage exclusively first, then starts this first remaining case.
    pytest.param(
        operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control,
        marks=[pytest.mark.scripted_model, pytest.mark.timeout(600)],
    ),
    pytest.param(
        an_identity_creates_a_native_session_only_with_an_explicit_binding_grant, marks=pytest.mark.scripted_model
    ),
    pytest.param(an_agent_messages_another_which_reads_and_acknowledges_it, marks=pytest.mark.scripted_model),
    pytest.param(a_policy_granted_after_the_sandbox_is_running_takes_effect, marks=pytest.mark.scripted_model),
    pytest.param(the_model_call_itself_goes_through_the_proxy, marks=pytest.mark.scripted_model),
    pytest.param(sandbox_cli_reaches_the_dispatcher_with_its_workload_svid, marks=pytest.mark.scripted_model),
    pytest.param(a_preset_launches_an_initialized_editable_codex_thread, marks=pytest.mark.scripted_model),
    pytest.param(
        pod_mtls_session_survives_replacement_and_keeps_history,
        marks=[pytest.mark.scripted_model, pytest.mark.timeout(600)],
    ),
    pytest.param(fresh_workspace_storage_survives_replacement),
    pytest.param(
        the_standing_instruction_reaches_the_model_and_only_in_its_own_session, marks=pytest.mark.scripted_model
    ),
    pytest.param(codex_standing_instructions_after_suspend_and_compact, marks=pytest.mark.scripted_model),
    pytest.param(two_harness_threads_keep_their_context_after_sandbox_suspend_resume, marks=pytest.mark.scripted_model),
    pytest.param(an_unknown_history_session_is_not_found),
    # Adjacent History readers reuse the harness pair after the other cases release their Sandboxes.
    pytest.param(
        every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor, marks=pytest.mark.scripted_model
    ),
    pytest.param(
        turns_nobody_watches_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn,
        marks=pytest.mark.scripted_model,
    ),
)
