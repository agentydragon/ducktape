"""Check native systemd supervision through ordinary Session setup and History."""

from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.session_setup import run_setup
from agentplane.system_tests.vm.guest_setup_probe import VM_SETUP_PROBE, assert_vm_setup_probe, setup_fence_probe


async def session_setup_is_supervised(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox_name: str,
    sandbox_uid: str,
    harness: session_pb2.Harness,
    model: str,
) -> Agent:
    session_id = uuid4()
    # A fresh nested path exercises Runner's workspace helper rather than a pre-created cwd.
    cwd = f"/state/workspaces/{session_id}/nested"
    stdout, stderr = await run_setup(
        client,
        dispatcher,
        sandbox_name=sandbox_name,
        sandbox_uid=sandbox_uid,
        session_id=session_id,
        harness=harness,
        model=model,
        cwd=cwd,
        setup_script=VM_SETUP_PROBE,
    )
    assert_vm_setup_probe(stdout, stderr, cwd=cwd)
    # A second setup observes the first one's cleanup from outside its execution domain.
    # Checking the kernel domain also catches descendants missed by a PID-only assertion.
    checked, errors = await run_setup(
        client,
        dispatcher,
        sandbox_name=sandbox_name,
        sandbox_uid=sandbox_uid,
        session_id=uuid4(),
        harness=harness,
        model=model,
        cwd=cwd,
        setup_script=setup_fence_probe(stdout),
    )
    assert checked == "SETUP_DOMAIN_EMPTY\n", (checked, errors)
    assert not errors, errors
    return Agent(client, dispatcher, thread_id=session_id, cursor=0, sandbox=sandbox_name, sandbox_uid=sandbox_uid)


# gazelle:include_dep @pypi//protobuf
