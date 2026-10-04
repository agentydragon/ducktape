"""Offline coverage for the live smoke's generated-config boundary."""

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from agentplane.acceptance.ollama_catalog import ollama_cases
from agentplane.runner import protocol_pb2
from util.bazel.runfiles import get_required_path

# gazelle:include_dep @pypi//protobuf


def _manifest(tmp_path: Path, harnesses: dict[str, list[str]]) -> Path:
    routes = list(dict.fromkeys(route for offered in harnesses.values() for route in offered))
    config = {
        "models": {
            "models": [{"model": route, "display_name": route, "reasoning_efforts": []} for route in routes],
            "harnesses": harnesses,
        }
    }
    path = tmp_path / "app.yaml"
    path.write_text(
        yaml.safe_dump_all(
            [
                {"kind": "Service", "metadata": {"name": "agentplane-app", "namespace": "agentplane-testing"}},
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "agentplane-app-config", "namespace": "agentplane-testing"},
                    "data": {"config.yaml": yaml.safe_dump(config, sort_keys=False)},
                },
            ]
        )
    )
    return path


def test_cases_use_offered_harnesses_and_prioritize_128k(tmp_path: Path) -> None:
    large = "ollama/oai-chat/example-256k"
    small = "ollama/oai-chat/example-128k"
    path = _manifest(tmp_path, {"HARNESS_CLAUDE": [large, small, "other/messages/example"], "HARNESS_CODEX": [small]})
    assert ollama_cases(path) == [
        (protocol_pb2.HARNESS_CLAUDE, small),
        (protocol_pb2.HARNESS_CODEX, small),
        (protocol_pb2.HARNESS_CLAUDE, large),
    ]


def test_no_ollama_cases_fails_instead_of_silently_skipping(tmp_path: Path) -> None:
    path = _manifest(tmp_path, {"HARNESS_CLAUDE": ["other/messages/example"], "HARNESS_CODEX": []})
    with pytest.raises(ValueError, match="no Ollama smoke cases"):
        ollama_cases(path)


def test_committed_app_config_supplies_smoke_cases() -> None:
    path = get_required_path("ducktape/cluster/generated/agentplane-testing/agentplane-testing.k8s.yaml")
    cases = ollama_cases(path)
    assert len(cases) == len(set(cases))
    assert {harness for harness, _ in cases} == {protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX}


if __name__ == "__main__":
    pytest_bazel.main()
