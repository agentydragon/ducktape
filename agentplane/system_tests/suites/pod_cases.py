"""Pod scenario bindings with fresh Sessions and explicit runner ownership."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.agent import TURN_SECONDS
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
    thread_stream,
    workload_trust,
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
            lambda harness, model, sandboxes: dispatcher_workload.sandbox_cli_lists_its_svid_created_session(
                client, resources.dispatcher, sandboxes, harness, model, resources.scripted
            ),
        )


async def a_preset_launches_an_initialized_editable_codex_thread(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await launch_presets.a_preset_launches_an_initialized_editable_codex_thread(
            client, resources.dispatcher, sandboxes, resources.scripted
        )


async def cert_manager_reissues_a_deleted_tls_secret(resources: Resources) -> None:
    await runner_credentials.cert_manager_reissues_a_deleted_tls_secret(resources.cluster)


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


async def agent_resumes_an_idle_suspended_pod_with_its_execution_account(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await suspend_resume.agent_resumes_an_idle_suspended_pod_with_its_execution_account(
            resources.cluster, client, resources.dispatcher, sandboxes, resources.scripted
        )


async def an_unknown_thread_is_not_found(resources: Resources) -> None:
    async with resources.http() as http:
        await thread_stream.an_unknown_thread_is_not_found(http)


async def every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event(resources: Resources) -> None:
    async with resources.shared() as (client, runners), resources.http() as http:
        await run_harness_pair(
            client,
            lambda harness, model: thread_stream.every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event(
                client, resources.dispatcher, http, runners.for_harness(harness), harness, model, resources.scripted
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
                    client, resources.dispatcher, http, runners.for_harness(harness), harness, model, resources.scripted
                )
            ),
        )


async def a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(resources: Resources) -> None:
    async with resources.shared() as (_, runners):
        await workload_trust.a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(resources.cluster, runners.claude)


async def a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(resources: Resources) -> None:
    async with resources.shared() as (_, runners):
        await workload_trust.a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(
            resources.cluster, runners.claude, route=workload_trust.RunnerControlRoute.PORT_FORWARD
        )


async def a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway(resources: Resources) -> None:
    async with resources.shared() as (_, runners):
        await workload_trust.a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway(
            resources.cluster, runners.claude
        )


async def a_non_sandbox_command_box_keeps_its_bearer_egress(resources: Resources) -> None:
    async with resources.open() as (_, sandboxes):
        caller = await sandboxes("systest-bearer-caller")
        await egress.a_non_sandbox_command_box_keeps_its_bearer_egress(resources.cluster, caller)


async def a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes(resources: Resources) -> None:
    async with resources.open() as (_, sandboxes):
        subject = await sandboxes("systest-standing-subject")
        await egress.a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes(resources.cluster, subject)


async def two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(resources: Resources) -> None:
    async with resources.open() as (client, sandboxes):
        await egress.two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(resources.cluster, client, sandboxes)


SCENARIOS = (
    # CaseBatch runs storage exclusively first, then starts this first remaining case.
    pytest.param(
        agent_resumes_an_idle_suspended_pod_with_its_execution_account,
        marks=[pytest.mark.scripted_model, pytest.mark.timeout(600)],
    ),
    pytest.param(a_non_sandbox_command_box_keeps_its_bearer_egress),
    pytest.param(a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes),
    pytest.param(two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it),
    pytest.param(an_agent_messages_another_which_reads_and_acknowledges_it, marks=pytest.mark.scripted_model),
    pytest.param(a_policy_granted_after_the_sandbox_is_running_takes_effect, marks=pytest.mark.scripted_model),
    pytest.param(the_model_call_itself_goes_through_the_proxy, marks=pytest.mark.scripted_model),
    pytest.param(sandbox_cli_reaches_the_dispatcher_with_its_workload_svid, marks=pytest.mark.scripted_model),
    pytest.param(a_preset_launches_an_initialized_editable_codex_thread, marks=pytest.mark.scripted_model),
    pytest.param(cert_manager_reissues_a_deleted_tls_secret),
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
    pytest.param(an_unknown_thread_is_not_found),
    # The node holds five Sandboxes beside the stack, and the runner pool keeps every pair it creates
    # until the suite ends. Two `shared` cases running together make a second pair, and an open case's
    # two Sandboxes do not fit beside both, so adjacent `shared` cases go last.
    pytest.param(a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused),
    pytest.param(a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner),
    pytest.param(a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway),
    pytest.param(
        every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event, marks=pytest.mark.scripted_model
    ),
    pytest.param(
        turns_nobody_watches_are_archived_and_a_failed_one_leaves_the_next_input_its_own_turn,
        marks=pytest.mark.scripted_model,
    ),
)
