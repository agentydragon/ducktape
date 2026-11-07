"""Sandbox-origin Session Dispatcher requests over its workload SVID route."""

from __future__ import annotations

import json
import shlex
from uuid import uuid4

from google.protobuf import json_format

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import WORKING_DIRECTORY, Agent
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream

_CLI = "/run/agentplane/environment/bin/agentplane"


async def sandbox_cli_lists_its_svid_created_session(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-dispatcher-svid-{uuid4().hex[:8]}")
    assert view.kubernetes_diagnostics.HasField("pod")
    agent = await Agent.create(
        client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=offered_model
    )

    session_id = str(uuid4())
    spec = session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=view.target.name, uid=view.target.uid), harness=harness
            )
        ),
        cwd=WORKING_DIRECTORY,
        model=offered_model,
        reasoning_effort="low",
    )
    create = shlex.join(
        [
            _CLI,
            "--json",
            "sessions",
            "create",
            session_id,
            "--sandbox",
            view.target.name,
            "--sandbox-uid",
            view.target.uid,
            "--spec",
            json_format.MessageToJson(spec),
        ]
    )
    list_sessions = shlex.join([_CLI, "--json", "sessions", "list"])
    command = "\n".join(
        ("set -eu", f"created=$({create})", f"listed=$({list_sessions})", 'printf \'%s\\n%s\\n\' "$created" "$listed"')
    )

    turn = await agent.run_shell(scripted.model(view.kubernetes_diagnostics.pod.name, harness), command)
    outputs = "\n".join(turn.tool_outputs).strip().splitlines()
    assert len(outputs) == 2, f"expected create and list JSON outputs, got {turn.tool_outputs!r}"
    created_text, listed_text = outputs
    created = json.loads(created_text)
    listed = json.loads(listed_text)
    assert created["sessionId"] == session_id, created
    assert any(row["sessionId"] == session_id for row in listed["sessions"]), listed
