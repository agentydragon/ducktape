"""Start one session-scoped stack from the suite's declared Bazel artifacts.

Suite fixtures expose the stack's explicit cluster, app URL, token, and MCP access.
The process trust bundle also includes Dex's CA for HTTP clients used by MCP scenarios.
"""

import os
from collections.abc import Iterator

import pytest

from agentplane.system_tests.k3s import guest_logs, rendered_fixture
from agentplane.system_tests.k3s.rendered_fixture import Backend, LocalModel, PublicLiteLLM
from agentplane.system_tests.k3s.stack import Stack, StackInputs, node_ip, running_stack
from util.bazel.runfiles import get_required_path
from util.net import Listener, listen_tcp
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="session")
def scripted_listener() -> Iterator[Listener]:
    """Retain the scripted model's binding until the deployment and all scenarios stop."""
    with listen_tcp(host=node_ip()) as listener:
        yield listener


def load_stack_inputs() -> StackInputs:
    return StackInputs(
        fixture=rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"])),
        binary=get_required_path(os.environ["K3S_BINARY_RLOCATION"]),
        image_layouts=tuple(get_required_path(path) for path in os.environ["K3S_IMAGE_LAYOUTS"].split()),
        guest_image_layouts=tuple(get_required_path(path) for path in os.environ["K3S_GUEST_IMAGE_LAYOUTS"].split()),
    )


def _models(inputs: StackInputs, listener: Listener) -> rendered_fixture.Models:
    match inputs.fixture.backend:
        case Backend.LOCAL_MODEL:
            return LocalModel(url=f"http://{listener.host}:{listener.port}")
        case Backend.PUBLIC_LITELLM:
            return PublicLiteLLM(key=os.environ["OPENAI_API_KEY"])


@pytest.fixture(scope="session", autouse=True)
def deployment(request: pytest.FixtureRequest, scripted_listener: Listener) -> Iterator[Stack]:
    inputs = load_stack_inputs()
    required_backend = "scripted_model" if inputs.fixture.backend is Backend.LOCAL_MODEL else "real_model"
    incompatible = "real_model" if required_backend == "scripted_model" else "scripted_model"
    for item in request.session.items:
        if item.get_closest_marker(incompatible) is not None:
            raise pytest.UsageError(f"{item.nodeid} requires {incompatible}; suite stack serves {required_backend}")
    with (
        running_stack(
            inputs,
            models=_models(inputs, scripted_listener),
            validate_vm_shutdown=lambda: request.session.testsfailed == 0,
        ) as stack,
        pytest.MonkeyPatch.context() as environment,
    ):
        environment.setenv(guest_logs.CONSOLES_ENV, str(undeclared_outputs_dir() / "vm-artifacts/consoles"))
        if stack.mcp_fixtures is not None:
            environment.setenv("SSL_CERT_FILE", str(stack.mcp_fixtures.ca_bundle))
        yield stack
