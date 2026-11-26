"""Register the deployment and KubeVirt runtime fixtures."""

from agentplane.system_tests.deployment.pytest_plugin import runtime_factory, scripted_listener, stack_inputs
from agentplane.system_tests.vm.pytest_plugin import kubevirt_runtime_factory
