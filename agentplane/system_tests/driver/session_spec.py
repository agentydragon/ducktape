"""Session specifications for the system tests' environment-hosted harnesses."""

from agentplane.proto import session_pb2

# gazelle:include_dep @pypi//protobuf


def environment_session_spec(
    sandbox: str, sandbox_uid: str, *, harness: session_pb2.Harness, model: str, cwd: str, instructions: str = ""
) -> session_pb2.SessionSpec:
    return session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=sandbox, uid=sandbox_uid), harness=harness
            )
        ),
        model=model,
        cwd=cwd,
        reasoning_effort="low",
        instructions=instructions,
    )
