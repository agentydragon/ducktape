"""Check native systemd supervision through ordinary Session setup and History."""

from uuid import uuid4

from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.session import Session
from agentplane.system_tests.vm.guest_setup_probe import VM_SETUP_PROBE, assert_vm_setup_probe, setup_fence_probe


async def session_setup_is_supervised(
    history: HistoryServiceClient,
    dispatcher: SessionDispatcherClient,
    sandbox_name: str,
    sandbox_uid: str,
    harness: session_pb2.Harness,
    model: str,
) -> Session:
    session_id = uuid4()
    # A fresh nested path exercises Runner's workspace helper rather than a pre-created cwd.
    cwd = f"/state/workspaces/{session_id}/nested"
    spec = session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=sandbox_name, uid=sandbox_uid), harness=harness
            )
        ),
        model=model,
        reasoning_effort="low",
        cwd=cwd,
    )
    session = await Session.create(dispatcher, history, spec=spec, session_id=session_id, setup_script=VM_SETUP_PROBE)
    stdout, stderr = await session.setup_output()
    assert_vm_setup_probe(stdout, stderr, cwd=cwd)
    # A second setup observes the first one's cleanup from outside its execution domain.
    # Checking the kernel domain also catches descendants missed by a PID-only assertion.
    fence = await Session.create(
        dispatcher, history, spec=spec, session_id=uuid4(), setup_script=setup_fence_probe(stdout)
    )
    checked, errors = await fence.setup_output()
    assert checked == "SETUP_DOMAIN_EMPTY\n", (checked, errors)
    assert not errors, errors
    return session


# gazelle:include_dep @pypi//protobuf
