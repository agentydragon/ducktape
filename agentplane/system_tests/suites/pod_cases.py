"""Pod scenario bindings with fresh Sessions and explicit runner ownership."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from agentplane.app.testing.client import Client
from agentplane.system_tests import scenario
from agentplane.system_tests.agent import TURN_SECONDS
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.executor import Executor
from agentplane.system_tests.harness_pair import run_harness_pair
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scenarios import (
    agent_messaging,
    egress,
    launch_presets,
    runner_credentials,
    session_instructions,
    storage,
    suspend_resume,
    thread_stream,
)
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites import pod_runners
from agentplane.system_tests.tracing import tracer


@dataclass(frozen=True)
class Resources:
    cluster: Cluster
    base_url: str
    token: str
    scripted: ScriptedUpstream
    borrow: pod_runners.Borrow

    @asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[Client, Sandboxes]]:
        async with (
            self.scripted.case_scope(),
            scenario.open_resources(self.cluster, self.base_url, self.token, tracer, native_diagnostics=False) as owned,
        ):
            yield owned

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[tuple[Client, pod_runners.Pair]]:
        # Settle the model queues before returning this pair to another case.
        async with (
            self.borrow() as runners,
            self.scripted.case_scope(),
            Client(base_url=self.base_url, token=self.token) as client,
        ):
            yield client, runners

    @asynccontextmanager
    async def http(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(thread_stream.REQUEST_SECONDS, read=TURN_SECONDS),
        ) as client:
            yield client


type Scenario = Callable[[Resources], Awaitable[None]]


async def an_agent_messages_another_which_reads_and_acknowledges_it(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await run_harness_pair(
            client,
            lambda harness, model: agent_messaging.an_agent_messages_another_which_reads_and_acknowledges_it(
                resources.cluster, client, sandboxes, harness, model, resources.scripted
            ),
        )


async def a_policy_granted_after_the_sandbox_is_running_takes_effect(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await run_harness_pair(
            client,
            lambda harness, model: egress.a_policy_granted_after_the_sandbox_is_running_takes_effect(
                resources.cluster, client, sandboxes, harness, model, resources.scripted
            ),
        )


async def the_model_call_itself_goes_through_the_proxy(resources: Resources) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: egress.the_model_call_itself_goes_through_the_proxy(
                resources.cluster, client, runners.for_harness(harness), harness, model, resources.scripted
            ),
        )


async def a_preset_launches_an_initialized_editable_codex_thread(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await launch_presets.a_preset_launches_an_initialized_editable_codex_thread(
            client, sandboxes, resources.scripted
        )


async def cert_manager_reissues_a_deleted_tls_secret(resources: Resources) -> None:
    await runner_credentials.cert_manager_reissues_a_deleted_tls_secret(resources.cluster)


async def pod_mtls_session_survives_replacement_and_keeps_history(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        async with asyncio.timeout(600):
            await run_harness_pair(
                client,
                lambda harness, model: runner_credentials.pod_mtls_session_survives_replacement_and_keeps_history(
                    resources.cluster, client, sandboxes, harness, model, resources.scripted
                ),
            )


async def the_standing_instruction_reaches_the_model_and_only_in_its_own_session(resources: Resources) -> None:
    async with resources.shared() as (client, runners):
        await run_harness_pair(
            client,
            lambda harness, model: (
                session_instructions.the_standing_instruction_reaches_the_model_and_only_in_its_own_session(
                    client, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


async def fresh_workspace_storage_survives_replacement(resources: Resources) -> None:
    async with resources.open() as (client, _sandboxes):
        await storage.fresh_workspace_storage_survives_replacement(
            resources.cluster,
            client,
            Executor(template="runner", cwd="/state/work"),
            storage.StorageLayout.POD_DIRECTORY,
        )


async def codex_standing_instructions_after_suspend_and_compact(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.codex_standing_instructions_after_suspend_and_compact(
            resources.cluster, client, sandboxes, resources.scripted
        )


async def two_harness_threads_keep_their_context_after_sandbox_suspend_resume(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
            resources.cluster, client, sandboxes, resources.scripted, Executor(template="runner", cwd="/state/work")
        )


async def an_unknown_thread_is_not_found(resources: Resources) -> None:
    async with resources.http() as http:
        await thread_stream.an_unknown_thread_is_not_found(http)


async def every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event(resources: Resources) -> None:
    async with resources.shared() as (client, runners), resources.http() as http:
        await run_harness_pair(
            client,
            lambda harness, model: thread_stream.every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event(
                client, http, runners.for_harness(harness), harness, model, resources.scripted
            ),
        )


async def turns_nobody_watches_are_archived_and_a_failed_one_leaves_the_next_input_its_own_turn(
    resources: Resources,
) -> None:
    async with resources.shared() as (client, runners), resources.http() as http:
        await run_harness_pair(
            client,
            lambda harness, model: (
                thread_stream.turns_nobody_watches_are_archived_and_a_failed_one_leaves_the_next_input_its_own_turn(
                    client, http, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


SCENARIOS = (
    pytest.param(an_agent_messages_another_which_reads_and_acknowledges_it, marks=pytest.mark.scripted_model),
    pytest.param(a_policy_granted_after_the_sandbox_is_running_takes_effect, marks=pytest.mark.scripted_model),
    pytest.param(the_model_call_itself_goes_through_the_proxy, marks=pytest.mark.scripted_model),
    pytest.param(a_preset_launches_an_initialized_editable_codex_thread, marks=pytest.mark.scripted_model),
    pytest.param(cert_manager_reissues_a_deleted_tls_secret),
    pytest.param(
        pod_mtls_session_survives_replacement_and_keeps_history,
        marks=[pytest.mark.scripted_model, pytest.mark.timeout(600)],
    ),
    pytest.param(
        the_standing_instruction_reaches_the_model_and_only_in_its_own_session, marks=pytest.mark.scripted_model
    ),
    pytest.param(fresh_workspace_storage_survives_replacement),
    pytest.param(codex_standing_instructions_after_suspend_and_compact, marks=pytest.mark.scripted_model),
    pytest.param(two_harness_threads_keep_their_context_after_sandbox_suspend_resume, marks=pytest.mark.scripted_model),
    pytest.param(an_unknown_thread_is_not_found),
    pytest.param(
        every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event, marks=pytest.mark.scripted_model
    ),
    pytest.param(
        turns_nobody_watches_are_archived_and_a_failed_one_leaves_the_next_input_its_own_turn,
        marks=pytest.mark.scripted_model,
    ),
)
