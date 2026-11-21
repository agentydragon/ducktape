"""Pod scenario bindings with fresh Sessions and explicit runner ownership."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.harness_pair import run_harness_pair, run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.native_platform import sandbox_harness_host
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.scenarios import (
    agent_messaging,
    dispatcher_workload,
    egress,
    launch_presets,
    runner_credentials,
    runtime_proxy,
    session_instructions,
    storage,
    suspend_resume,
    thread_history,
)
from agentplane.system_tests.suites.pod_worker import Worker

type Scenario = Callable[[Worker], Awaitable[None]]


async def environments_forwards_runtime_reads_and_refuses_stale_incarnations(worker: Worker) -> None:
    async with worker.reused_sandbox() as case:
        await runtime_proxy.environments_forwards_runtime_reads_and_refuses_stale_incarnations(
            worker.cluster, case.client, case.sandbox
        )


async def an_agent_messages_another_which_reads_and_acknowledges_it(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await run_harness_pair_in_own_sandboxes(
            worker.cluster,
            case.client,
            lambda harness, model, sandboxes: agent_messaging.an_agent_messages_another_which_reads_and_acknowledges_it(
                worker.cluster, case.client, worker.dispatcher, sandboxes, harness, model, worker.scripted
            ),
        )


async def a_policy_granted_after_the_sandbox_is_running_takes_effect(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await run_harness_pair_in_own_sandboxes(
            worker.cluster,
            case.client,
            lambda harness, model, sandboxes: egress.a_policy_granted_after_the_sandbox_is_running_takes_effect(
                worker.cluster, case.client, worker.dispatcher, sandboxes, harness, model, worker.scripted
            ),
        )


async def the_model_call_itself_goes_through_the_proxy(worker: Worker) -> None:
    async with worker.reused_sandbox() as case:
        await run_harness_pair(
            case.client,
            lambda harness, model: egress.the_model_call_itself_goes_through_the_proxy(
                worker.cluster, case.client, worker.dispatcher, case.sandbox, harness, model, worker.scripted
            ),
        )


async def sandbox_cli_reaches_the_dispatcher_with_its_workload_svid(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await run_harness_pair_in_own_sandboxes(
            worker.cluster,
            case.client,
            lambda harness, model, sandboxes: (
                dispatcher_workload.sandbox_cli_denies_create_but_lists_its_identity_session(
                    case.client, worker.dispatcher, sandboxes, harness, model, worker.scripted
                )
            ),
        )


async def a_preset_launches_an_initialized_editable_codex_thread(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await launch_presets.a_preset_launches_an_initialized_editable_codex_thread(
            case.client, worker.dispatcher, case.sandboxes, worker.scripted
        )


async def pod_mtls_session_survives_replacement_and_keeps_history(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        async with asyncio.timeout(600):
            await run_harness_pair_in_own_sandboxes(
                worker.cluster,
                case.client,
                lambda harness, model, sandboxes: (
                    runner_credentials.pod_mtls_session_survives_replacement_and_keeps_history(
                        worker.cluster, case.client, worker.dispatcher, sandboxes, harness, model, worker.scripted
                    )
                ),
            )


async def the_standing_instruction_reaches_the_model_and_only_in_its_own_session(worker: Worker) -> None:
    async with worker.reused_sandbox() as case:
        await run_harness_pair(
            case.client,
            lambda harness, model: (
                session_instructions.the_standing_instruction_reaches_the_model_and_only_in_its_own_session(
                    case.client, worker.dispatcher, case.sandbox, harness, model, worker.scripted
                )
            ),
        )


async def fresh_workspace_storage_survives_replacement(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await storage.fresh_workspace_storage_survives_replacement(
            worker.cluster,
            case.client,
            worker.dispatcher,
            Executor(template=POD_TEMPLATE, cwd="/state/work"),
            vm_storage=None,
        )


async def codex_standing_instructions_after_suspend_and_compact(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case, sandbox_harness_host(worker.cluster) as control:
        await suspend_resume.codex_standing_instructions_after_suspend_and_compact(
            worker.cluster, case.client, worker.dispatcher, control, case.sandboxes, worker.scripted
        )


async def two_harness_threads_keep_their_context_after_sandbox_suspend_resume(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case, sandbox_harness_host(worker.cluster) as control:
        await suspend_resume.two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
            worker.cluster,
            case.client,
            worker.dispatcher,
            control,
            case.sandboxes,
            worker.scripted,
            Executor(template=POD_TEMPLATE, cwd="/state/work"),
        )


async def operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await suspend_resume.operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control(
            worker.cluster, case.client, worker.dispatcher, case.sandboxes, worker.scripted
        )


async def an_identity_creates_a_native_session_only_with_an_explicit_binding_grant(worker: Worker) -> None:
    async with worker.fresh_sandboxes() as case:
        await suspend_resume.an_identity_creates_a_native_session_only_with_an_explicit_binding_grant(
            worker.cluster, case.client, worker.dispatcher, case.sandboxes, worker.scripted
        )


async def an_unknown_history_session_is_not_found(worker: Worker) -> None:
    async with Client.connect(base_url=worker.base_url, token=worker.token, dispatcher=worker.dispatcher) as client:
        await thread_history.an_unknown_history_session_is_not_found(client)


async def every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor(worker: Worker) -> None:
    async with worker.reused_sandbox() as case:
        await run_harness_pair(
            case.client,
            lambda harness, model: (
                thread_history.every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor(
                    case.client, worker.dispatcher, case.sandbox, harness, model, worker.scripted
                )
            ),
        )


async def turns_nobody_watches_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn(
    worker: Worker,
) -> None:
    async with worker.reused_sandbox() as case:
        await run_harness_pair(
            case.client,
            lambda harness, model: (
                thread_history.unwatched_turns_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn(
                    case.client, worker.dispatcher, case.sandbox, harness, model, worker.scripted
                )
            ),
        )


SCENARIOS = (
    pytest.param(environments_forwards_runtime_reads_and_refuses_stale_incarnations),
    pytest.param(the_model_call_itself_goes_through_the_proxy, marks=pytest.mark.scripted_model),
    pytest.param(
        the_standing_instruction_reaches_the_model_and_only_in_its_own_session, marks=pytest.mark.scripted_model
    ),
    pytest.param(
        every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor, marks=pytest.mark.scripted_model
    ),
    pytest.param(
        turns_nobody_watches_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn,
        marks=pytest.mark.scripted_model,
    ),
    # CaseBatch runs storage exclusively before the bounded shared/fresh case batch.
    pytest.param(
        operator_resumes_an_idle_pod_without_granting_its_identity_lifecycle_control,
        marks=[pytest.mark.scripted_model, pytest.mark.case_timeout(600)],
    ),
    pytest.param(
        an_identity_creates_a_native_session_only_with_an_explicit_binding_grant, marks=pytest.mark.scripted_model
    ),
    pytest.param(an_agent_messages_another_which_reads_and_acknowledges_it, marks=pytest.mark.scripted_model),
    pytest.param(a_policy_granted_after_the_sandbox_is_running_takes_effect, marks=pytest.mark.scripted_model),
    pytest.param(sandbox_cli_reaches_the_dispatcher_with_its_workload_svid, marks=pytest.mark.scripted_model),
    pytest.param(a_preset_launches_an_initialized_editable_codex_thread, marks=pytest.mark.scripted_model),
    pytest.param(
        pod_mtls_session_survives_replacement_and_keeps_history,
        marks=[pytest.mark.scripted_model, pytest.mark.case_timeout(600)],
    ),
    pytest.param(fresh_workspace_storage_survives_replacement, marks=pytest.mark.exclusive_case),
    pytest.param(codex_standing_instructions_after_suspend_and_compact, marks=pytest.mark.scripted_model),
    pytest.param(two_harness_threads_keep_their_context_after_sandbox_suspend_resume, marks=pytest.mark.scripted_model),
    pytest.param(an_unknown_history_session_is_not_found),
)
