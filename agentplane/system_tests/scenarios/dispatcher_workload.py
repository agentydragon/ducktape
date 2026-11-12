"""Sandbox-origin Session Dispatcher requests over its workload SVID route."""

from __future__ import annotations

import json
import shlex
from uuid import uuid4

from google.protobuf import json_format
from more_itertools import one

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import WORKING_DIRECTORY, Agent
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream

_CLI = "/run/agentplane/environment/bin/agentplane"


async def sandbox_cli_denies_create_but_reads_its_identity_session(
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
        client,
        dispatcher,
        sandbox=view.target.name,
        sandbox_uid=view.target.uid,
        harness=harness,
        model=offered_model,
        instructions=f"SVID-SESSION-{uuid4()}",
    )

    session_id = str(uuid4())
    existing_session_id = str(agent.thread_id)
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
    read_existing = shlex.join([_CLI, "--json", "sessions", "get", existing_session_id])
    command = "\n".join(
        (
            "set -eu",
            f"if created=$({create}); then :; else :; fi",
            f"read=$({read_existing})",
            f"listed=$({list_sessions})",
            'printf \'%s\\n%s\\n%s\\n\' "$created" "$read" "$listed"',
        )
    )

    turn = await agent.run_shell(scripted.model(view.kubernetes_diagnostics.pod.name, harness), command)
    outputs = [line for line in "\n".join(turn.tool_outputs).splitlines() if line.strip()]
    assert len(outputs) == 3, f"expected create, get and list JSON outputs, got {turn.tool_outputs!r}"
    refused_create = json.loads(outputs[0])
    read = json.loads(outputs[1])
    listed = json.loads(outputs[2])
    assert refused_create["error"]["code"] == "PERMISSION_DENIED", refused_create

    rows = listed["sessions"]
    assert not any(row["sessionId"] == session_id for row in rows), listed
    existing = one(row for row in rows if row["sessionId"] == existing_session_id)
    assert read["session_id"] == existing_session_id
    assert read["session"]["kind"] == "observed"
    current_identity = read["session"]["value"]["identity"]
    assert current_identity["name"] == view.identity_name
    assert current_identity["uid"]
    assert existing["identity"] == current_identity
