"""Start one session-scoped stack from the suite's declared Bazel artifacts.

Suite fixtures expose the stack's explicit cluster, app URL, token, and MCP access.
The process trust bundle also includes Dex's CA for HTTP clients used by MCP scenarios.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.system_tests.deployment import rendered_fixture
from agentplane.system_tests.deployment.rendered_fixture import Backend, LocalModel, PublicLiteLLM
from agentplane.system_tests.deployment.stack import Stack, StackInputs, running_stack
from agentplane.system_tests.deployment.virtualization import VirtualizationFactory
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import node_ip
from util.net import Listener, listen_tcp


@pytest.fixture(scope="session")
def scripted_listener() -> Iterator[Listener]:
    """Retain the scripted model's binding until the deployment and all scenarios stop."""
    with listen_tcp(host=node_ip()) as listener:
        yield listener


def load_stack_inputs(runfiles: Runfiles, k3s_binary: Path) -> StackInputs:
    runfiles_root = runfiles.root()
    images = json.loads((runfiles_root / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]).read_text())
    return StackInputs(
        fixture=rendered_fixture.read(runfiles_root / os.environ["K3S_FIXTURE_RLOCATION"]),
        binary=k3s_binary,
        image_layouts=_image_layouts(runfiles, images["fixture"]),
        pki_image_layouts=_image_layouts(runfiles, images["pki"]),
        vm_image_layouts=_image_layouts(runfiles, images["vm"]),
        guest_image_layouts=_image_layouts(runfiles, images["guest"]),
    )


def _image_layouts(runfiles: Runfiles, images: dict[str, str]) -> dict[str, Path]:
    runfiles_root = runfiles.root()
    return {reference: runfiles_root / location for reference, location in images.items()}


def _models(inputs: StackInputs, listener: Listener) -> rendered_fixture.Models:
    match inputs.fixture.backend:
        case Backend.LOCAL_MODEL:
            return LocalModel(url=f"http://{listener.host}:{listener.port}")
        case Backend.PUBLIC_LITELLM:
            return PublicLiteLLM(key=os.environ["OPENAI_API_KEY"])


@pytest.fixture(scope="session")
def virtualization_factory() -> VirtualizationFactory | None:
    return None


@pytest.fixture(scope="session", autouse=True)
def deployment(
    request: pytest.FixtureRequest,
    scripted_listener: Listener,
    virtualization_factory: VirtualizationFactory | None,
    runfiles: Runfiles,
    k3s_binary: Path,
) -> Iterator[Stack]:
    inputs = load_stack_inputs(runfiles, k3s_binary)
    required_backend = "scripted_model" if inputs.fixture.backend is Backend.LOCAL_MODEL else "real_model"
    incompatible = "real_model" if required_backend == "scripted_model" else "scripted_model"
    for item in request.session.items:
        if item.get_closest_marker(incompatible) is not None:
            raise pytest.UsageError(f"{item.nodeid} requires {incompatible}; suite stack serves {required_backend}")
    with (
        running_stack(
            inputs,
            models=_models(inputs, scripted_listener),
            virtualization_factory=virtualization_factory,
            validate_vm_shutdown=lambda: request.session.testsfailed == 0,
            persist_infrastructure=os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") == "1",
        ) as stack,
        pytest.MonkeyPatch.context() as environment,
    ):
        if stack.mcp_fixtures is not None:
            environment.setenv("SSL_CERT_FILE", str(stack.mcp_fixtures.ca_bundle))
        yield stack


@pytest.fixture(scope="session")
def base_url(deployment: Stack) -> str:
    return deployment.base_url


@pytest.fixture(scope="session")
def token(deployment: Stack) -> str:
    return deployment.token


@pytest.fixture(scope="session")
def cluster(deployment: Stack) -> Cluster:
    return deployment.cluster
