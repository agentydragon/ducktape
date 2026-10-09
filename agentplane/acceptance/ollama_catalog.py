"""Live-smoke cases from the committed app config, not its deployment generator."""

from pathlib import Path

import yaml

from agentplane.app.model_catalog import ModelCatalog
from agentplane.runner import protocol_pb2
from agentplane.runner.harness import Harness

# gazelle:include_dep @pypi//protobuf


def ollama_cases(manifest: Path) -> list[tuple[protocol_pb2.Harness, str]]:
    [config_map] = [
        resource
        for resource in yaml.safe_load_all(manifest.read_text())
        if resource["kind"] == "ConfigMap"
        and resource["metadata"]["namespace"] == "agentplane-testing"
        and resource["metadata"]["name"] == "agentplane-app-config"
    ]
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    catalog = ModelCatalog.model_validate(config["models"])
    routes = sorted(
        (option.model for option in catalog.models if option.model.startswith("ollama/")),
        key=lambda route: not route.endswith("-128k"),
    )
    cases = [
        (harness, route)
        for route in routes
        for harness in (protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX)
        if route in catalog.harnesses[Harness(protocol_pb2.Harness.Name(harness))]
    ]
    if not cases:
        raise ValueError("committed Agentplane testing config offers no Ollama smoke cases")
    return cases
