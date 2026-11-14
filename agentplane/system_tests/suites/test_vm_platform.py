"""One deployed VM checks platform wiring through real setup, without model turns."""

import json
import shlex
from uuid import uuid4

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.scenario import ScenarioResources
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.session_setup import run_setup
from agentplane.system_tests.scenarios import runner_credentials
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.guest_logs import check_boot, systemd_supervision_passed
from agentplane.system_tests.vm.guest_setup_probe import VM_SETUP_PROBE, assert_vm_setup_probe, setup_fence_probe
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# session_pb2.pyi imports protobuf stubs during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_one_vm_platform_without_model_turns(
    client: Client,
    cluster: Cluster,
    dispatcher: SessionDispatcherClient,
    scenario_resources: ScenarioResources,
    scripted: ScriptedUpstream,
) -> None:
    # Retain the real scripted endpoint: any unexpected model request is archived
    # and fails its unanswered-request teardown. No response is scripted here.
    harness = session_pb2.HARNESS_CODEX
    model = (await models_offered_for(client, [harness]))[harness]
    output = undeclared_outputs_dir()
    async with scenario_resources() as (guest_client, create):
        view = await create("systest-vm-platform", template=SUPERVISION_TEMPLATE)
        assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
        assert view.observed_state.phase == environment_service_pb2.SANDBOX_PHASE_RUNNING
        assert view.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
        assert view.kubernetes_diagnostics.HasField("pod")
        identity = await runner_credentials.check_vm_runner_certificate(cluster, view)
        assert identity[0] == view.kubernetes_diagnostics.pod.uid
        assert identity[1]

        runtime = await guest_client.runner_status(view.target.name, view.target.uid)
        assert runtime.environment_runtime_boot_id
        assert runtime.lifecycle.desired_mode == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
        assert runtime.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
        assert runtime.lifecycle.progress_available
        assert runtime.lifecycle.progress_current
        assert not runtime.lifecycle.error
        assert not runtime.lifecycle.HasField("blocker")
        async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                check_boot()
                assert systemd_supervision_passed(view.kubernetes_diagnostics.pod.uid)

        session_id = uuid4()
        cwd = f"/state/workspaces/{session_id}/nested"
        marker = f"VM_PLATFORM_{uuid4().hex}"
        filename = f".platform-{uuid4().hex}"
        platform_script = "\n".join(
            [
                "printf 'PLATFORM_BEGIN\\n'",
                f"printf '%s\\n' {shlex.quote(marker)} > {shlex.quote(filename)}",
                f'test "$(cat {shlex.quote(filename)})" = {shlex.quote(marker)}',
                f"cat {shlex.quote(filename)}",
                f"rm -- {shlex.quote(filename)}",
                f"test ! -e {shlex.quote(filename)}",
                "printf 'EGRESS_RULES_JSON\\n'",
                "agentplane --json egress rules",
                "printf '\\nEGRESS_RULES_END\\n'",
                "agentplane notifications subscriptions --limit 1 >/dev/null",
                "printf 'NOTIFICATIONS_GRPC_OK\\n'",
            ]
        )
        stdout, stderr = await run_setup(
            guest_client,
            dispatcher,
            sandbox_name=view.target.name,
            sandbox_uid=view.target.uid,
            session_id=session_id,
            harness=harness,
            model=model,
            cwd=cwd,
            setup_script=VM_SETUP_PROBE + "\n" + platform_script,
        )
        supervision, separator, platform = stdout.partition("PLATFORM_BEGIN\n")
        assert separator, stdout
        assert_vm_setup_probe(supervision, stderr, cwd=cwd)
        assert platform.startswith(marker + "\nEGRESS_RULES_JSON\n"), platform
        rules = json.loads(platform.split("EGRESS_RULES_JSON\n", 1)[1].split("\nEGRESS_RULES_END\n", 1)[0])
        assert rules["identity"] == {"name": view.identity_name}, rules
        assert platform.endswith("NOTIFICATIONS_GRPC_OK\n"), platform

        fenced, errors = await run_setup(
            guest_client,
            dispatcher,
            sandbox_name=view.target.name,
            sandbox_uid=view.target.uid,
            session_id=uuid4(),
            harness=harness,
            model=model,
            cwd=cwd,
            setup_script=setup_fence_probe(supervision),
        )
        assert fenced == "SETUP_DOMAIN_EMPTY\n", fenced
        assert not errors, errors
        after = await guest_client.runner_status(view.target.name, view.target.uid)
        assert after.environment_runtime_boot_id == runtime.environment_runtime_boot_id
        assert after.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
        assert after.lifecycle.progress_current
        assert not after.lifecycle.error
        await runner_credentials.assert_vm_runner_identity_unchanged(
            cluster, view.kubernetes_diagnostics.pod.name, identity
        )
        (output / "vm-platform.json").write_text(
            json.dumps(
                {
                    "sandbox_uid": view.target.uid,
                    "pod_uid": identity[0],
                    "boot_id": runtime.environment_runtime_boot_id,
                    "workspace_marker": marker,
                    "egress_identity": rules["identity"],
                    "supervision_and_fence": True,
                    "notification_cli": True,
                },
                indent=2,
            )
            + "\n"
        )
    assert not list((output / "scripted_upstream").glob("*.json")), "platform witness made a model request"


if __name__ == "__main__":
    pytest_bazel.main()
