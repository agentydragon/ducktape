"""Selectable VM harness cases sharing one stack and two concurrent case slots."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from typing import cast

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.proto import session_pb2
from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import native_platform_clients
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import vm_sandbox
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.trace_contract import assert_vm_process_trace_contract
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/vm:pytest_plugin
pytest_plugins = ("agentplane.system_tests.vm.pytest_plugin",)

# session_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = [
    pytest.mark.vm_sandboxes,
    pytest.mark.scripted_model,
    pytest.mark.case_batch,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.case_timeout(600),
]


@dataclass(frozen=True)
class VmCase:
    harness: session_pb2.Harness
    template: str


CASES = (
    # Only Claude selects the private supervisor checks; both use the ordinary guest image.
    pytest.param(VmCase(session_pb2.HARNESS_CLAUDE, SUPERVISION_TEMPLATE), id="claude"),
    pytest.param(VmCase(session_pb2.HARNESS_CODEX, VM_TEMPLATE), id="codex"),
)


async def _wait_for_vm_process_trace_contract(sandbox_uids: set[str], *, require_codex: bool) -> None:
    output_dir = undeclared_outputs_dir()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    artifacts = [output_dir / "test-spans.jsonl", output_dir / "process-spans.jsonl"]
    last_sizes: tuple[int, ...] = ()
    error: AssertionError | None = None
    while True:
        current_sizes = tuple(path.stat().st_size if path.exists() else 0 for path in artifacts)
        if current_sizes != last_sizes:
            try:
                assert_vm_process_trace_contract(output_dir, sandbox_uids=sandbox_uids, require_codex=require_codex)
            except AssertionError as caught:
                error = caught
            else:
                return
            last_sizes = current_sizes
        if loop.time() >= deadline:
            raise AssertionError(f"VM process trace contract failed: {error}") from error
        await asyncio.sleep(0.5)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def vm_cases(
    request: pytest.FixtureRequest, cluster: Cluster, scripted_listener: Listener
) -> AsyncIterator[CaseBatch]:
    scripted = ScriptedUpstream()

    async def turn(case: VmCase, _worker: None) -> None:
        async with (
            scripted.case_scope(),
            native_platform_clients(cluster) as platform,
            native_sandbox_scope(cluster, platform) as sandboxes,
        ):
            uid = await vm_sandbox.vm_sandbox_turn(cluster, platform, sandboxes, case.harness, scripted, case.template)
        # Assert only this case's identities, so harness selection and a failed peer do
        # not force the other case to wait for evidence it cannot produce.
        await _wait_for_vm_process_trace_contract({uid}, require_codex=case.harness == session_pb2.HARNESS_CODEX)

    cases = {
        item.nodeid: Case.for_item(item, partial(turn, cast(VmCase, item.callspec.params["case"])))
        for item in selected_items(request)
    }
    async with (
        scripted.serving(listener=scripted_listener.socket),
        CaseBatch.running(cases, limit=2, worker_scope=nullcontext) as batch,
    ):
        yield batch


@pytest.mark.parametrize("case", CASES)
async def test_vm_scenarios(case: VmCase, vm_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await vm_cases.report(request)


if __name__ == "__main__":
    pytest_bazel.main()
