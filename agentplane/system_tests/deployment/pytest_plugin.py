"""Start one session-scoped stack from the suite's declared Bazel artifacts.

Suite fixtures expose the stack's explicit cluster, app URL, token, and MCP access.
The process trust bundle also includes Dex's CA for HTTP clients used by MCP scenarios.
"""

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.system_tests.deployment import rendered_fixture
from agentplane.system_tests.deployment.rendered_fixture import Backend, LocalModel, PublicLiteLLM, SandboxRuntime
from agentplane.system_tests.deployment.stack import Stack, StackInputs, running_stack
from agentplane.system_tests.deployment.virtualization import RuntimeFactory
from agentplane.system_tests.driver import scripted_upstream
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import node_ip

# All suites use the same scenario hooks, including batched-case duration reporting.
# gazelle:include_dep //agentplane/system_tests:conftest
from util.net import Listener, listen_tcp


@pytest.fixture(scope="session")
def scripted_listener() -> Iterator[Listener]:
    """Retain the scripted model's binding until the deployment and all scenarios stop."""
    with listen_tcp(host=node_ip()) as listener:
        yield listener


@pytest.fixture
async def scripted(scripted_listener: Listener) -> AsyncIterator[scripted_upstream.ScriptedUpstream]:
    """The model behind this target's LLM ingress, played by the scenario. One per test, so a request
    a scenario leaves unanswered fails that scenario."""
    upstream = scripted_upstream.ScriptedUpstream()
    async with upstream.serving(listener=scripted_listener.socket):
        yield upstream


def load_stack_inputs(runfiles: Runfiles, k3s_binary: Path) -> StackInputs:
    runfiles_root = runfiles.root()
    images = json.loads((runfiles_root / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]).read_text())
    seed = os.environ.get("K3S_INFRASTRUCTURE_IMAGE_SEED_RLOCATION")
    return StackInputs(
        fixture=rendered_fixture.read(runfiles_root / os.environ["K3S_FIXTURE_RLOCATION"]),
        binary=k3s_binary,
        image_layouts=_image_layouts(runfiles, images["fixture"]),
        pki_image_layouts=_image_layouts(runfiles, images["pki"]),
        vm_image_layouts=_image_layouts(runfiles, images["vm"]),
        guest_image_layouts=_image_layouts(runfiles, images["guest"]),
        cold_k3s=os.environ.get("AGENTPLANE_K3S_COLD") == "1",
        infrastructure_image_seed=runfiles_root / seed if seed else None,
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
def stack_inputs(runfiles: Runfiles, k3s_binary: Path) -> StackInputs:
    return load_stack_inputs(runfiles, k3s_binary)


@pytest.fixture(scope="session")
def runtime_factory(request: pytest.FixtureRequest, stack_inputs: StackInputs) -> RuntimeFactory | None:
    """Resolve the runtime declared in the manifest from the suite's pytest fixtures."""
    match stack_inputs.fixture.runtime:
        case SandboxRuntime.POD:
            return None
        case SandboxRuntime.KUBEVIRT:
            fixture_name = "kubevirt_runtime_factory"
        case SandboxRuntime.FIRECRACKER:
            fixture_name = "firecracker_runtime_factory"
    factory: RuntimeFactory = request.getfixturevalue(fixture_name)
    return factory


@pytest.fixture(scope="session")
def deployment(
    request: pytest.FixtureRequest,
    scripted_listener: Listener,
    runtime_factory: RuntimeFactory | None,
    stack_inputs: StackInputs,
) -> Iterator[Stack]:
    inputs = stack_inputs
    required_backend = "scripted_model" if inputs.fixture.backend is Backend.LOCAL_MODEL else "real_model"
    incompatible = "real_model" if required_backend == "scripted_model" else "scripted_model"
    for item in request.session.items:
        if item.get_closest_marker(incompatible) is not None:
            raise pytest.UsageError(f"{item.nodeid} requires {incompatible}; suite stack serves {required_backend}")
    with (
        running_stack(
            inputs,
            models=_models(inputs, scripted_listener),
            runtime_factory=runtime_factory,
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
    assert deployment.base_url is not None, "this fixture has no App"
    return deployment.base_url


@pytest.fixture(scope="session")
def token(deployment: Stack) -> str:
    assert deployment.token is not None, "this fixture has no App operator"
    return deployment.token


@pytest.fixture(scope="session")
def cluster(deployment: Stack) -> Cluster:
    return deployment.cluster
