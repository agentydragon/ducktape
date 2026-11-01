"""Select live-smoke cases from the model catalog offered by Agentplane."""

from agentplane.env.runtime.harnesshost.harness import Harness
from agentplane.proto import session_pb2
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog

# gazelle:include_dep @pypi//protobuf


def ollama_cases(catalog: ModelCatalog) -> list[tuple[session_pb2.Harness, str]]:
    routes = sorted(
        (option.model for option in catalog.models if option.model.startswith("ollama/")),
        key=lambda route: not route.endswith("-128k"),
    )
    cases = [
        (harness, route)
        for route in routes
        for harness in (session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX)
        if route in catalog.harnesses[Harness(session_pb2.Harness.Name(harness))]
    ]
    if not cases:
        raise ValueError("deployed model catalog offers no Ollama smoke cases")
    return cases
