"""Sandbox descriptors and template choices shared by native and App test drivers."""

from collections.abc import Callable, Coroutine
from typing import Any

from agentplane.environments.v1 import environment_service_pb2

POD_TEMPLATE = "environment-tools"
ENVIRONMENT_RUNTIME_CONTAINER = "environment-runtime"
VM_TEMPLATE = "environment-vm"
VM_WORKING_DIRECTORY = "/state/workspaces/accept"
SANDBOX_READY_SECONDS = 300.0
Sandboxes = Callable[..., Coroutine[Any, Any, environment_service_pb2.EnvironmentDescriptor]]
