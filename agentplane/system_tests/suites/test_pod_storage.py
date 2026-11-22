"""Workspace replacement on an isolated native Pod stack."""

from uuid import UUID

import pytest_bazel

from agentplane.proto import session_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.native_platform import native_platform_clients
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.session import Session
from agentplane.system_tests.scenarios.storage import fresh_workspace_storage_survives_replacement

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep @pypi//protobuf
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


async def test_fresh_workspace_storage_survives_replacement(cluster: Cluster) -> None:
    async with native_platform_clients(cluster) as platform:

        async def run_setup(
            *,
            sandbox_name: str,
            sandbox_uid: str,
            session_id: UUID,
            harness: session_pb2.Harness,
            model: str,
            cwd: str,
            setup_script: str,
        ) -> tuple[str, str]:
            session = await Session.create(
                platform.dispatcher,
                platform.history,
                session_id=session_id,
                setup_script=setup_script,
                spec=session_pb2.SessionSpec(
                    placement=session_pb2.SessionPlacement(
                        environment_hosted=session_pb2.EnvironmentHostedPlacement(
                            environment=session_pb2.ExactEnvironment(name=sandbox_name, uid=sandbox_uid),
                            harness=harness,
                        )
                    ),
                    cwd=cwd,
                    model=model,
                    reasoning_effort="low",
                ),
            )
            return await session.setup_output()

        await fresh_workspace_storage_survives_replacement(
            cluster, run_setup, Executor(template=POD_TEMPLATE, cwd="/state/work"), vm_storage=None
        )


if __name__ == "__main__":
    pytest_bazel.main()
