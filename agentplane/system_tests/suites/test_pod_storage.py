"""Workspace replacement on an isolated native Pod stack."""

import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.scenarios.storage import fresh_workspace_storage_survives_replacement

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


async def test_fresh_workspace_storage_survives_replacement(cluster: Cluster) -> None:
    await fresh_workspace_storage_survives_replacement(
        cluster, Executor(template=POD_TEMPLATE, cwd="/state/work"), vm_storage=None
    )


if __name__ == "__main__":
    pytest_bazel.main()
