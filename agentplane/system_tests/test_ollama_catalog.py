"""Offline coverage for selecting smoke cases from a model catalog."""

import pytest
import pytest_bazel

from agentplane.proto import session_pb2
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from agentplane.system_tests.ollama_catalog import ollama_cases

# gazelle:include_dep @pypi//protobuf


def _catalog(harnesses: dict[str, list[str]]) -> ModelCatalog:
    routes = list(dict.fromkeys(route for offered in harnesses.values() for route in offered))
    return ModelCatalog.model_validate(
        {
            "models": [{"model": route, "display_name": route, "reasoning_efforts": []} for route in routes],
            "harnesses": harnesses,
        }
    )


def test_cases_use_offered_harnesses_and_prioritize_128k() -> None:
    large = "ollama/oai-chat/example-256k"
    small = "ollama/oai-chat/example-128k"
    catalog = _catalog({"HARNESS_CLAUDE": [large, small, "other/messages/example"], "HARNESS_CODEX": [small]})
    assert ollama_cases(catalog) == [
        (session_pb2.HARNESS_CLAUDE, small),
        (session_pb2.HARNESS_CODEX, small),
        (session_pb2.HARNESS_CLAUDE, large),
    ]


def test_no_ollama_cases_fails_instead_of_silently_skipping() -> None:
    catalog = _catalog({"HARNESS_CLAUDE": ["other/messages/example"], "HARNESS_CODEX": []})
    with pytest.raises(ValueError, match="no Ollama smoke cases"):
        ollama_cases(catalog)


if __name__ == "__main__":
    pytest_bazel.main()
