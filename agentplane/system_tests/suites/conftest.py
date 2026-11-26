"""Register the deployment, runtime, App and native-platform fixtures the suites share."""

from agentplane.system_tests.app_fixtures import (
    client,
    dispatcher,
    harness,
    model,
    offered_model,
    pytest_runtest_makereport,
    sandbox,
    scenario_resources,
)
from agentplane.system_tests.deployment.pytest_plugin import (
    base_url,
    cluster,
    deployment,
    runtime_factory,
    scripted,
    scripted_listener,
    stack_inputs,
    token,
)
from agentplane.system_tests.driver.native_fixtures import native_platform, native_sandbox
from agentplane.system_tests.firecracker.pytest_plugin import firecracker_runtime_factory
from agentplane.system_tests.vm.pytest_plugin import kubevirt_runtime_factory
