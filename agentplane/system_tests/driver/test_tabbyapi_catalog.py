"""Offline coverage for selecting smoke cases from a model catalog."""

import pytest
import pytest_bazel

from agentplane.proto import session_pb2
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from agentplane.system_tests.driver.tabbyapi_catalog import tabbyapi_cases

# gazelle:include_dep @pypi//protobuf


def _catalog(harnesses: dict[str, list[str]]) -> ModelCatalog:
    routes = list(dict.fromkeys(route for offered in harnesses.values() for route in offered))
    return ModelCatalog.model_validate(
        {
            "models": [{"model": route, "display_name": route, "reasoning_efforts": []} for route in routes],
            "harnesses": harnesses,
        }
    )


def test_cases_use_offered_harnesses() -> None:
    large = "tabbyapi/oai-chat/example-b"
    small = "tabbyapi/oai-chat/example-a"
    catalog = _catalog({"HARNESS_CLAUDE": [large, small, "other/messages/example"], "HARNESS_CODEX": [small]})
    assert tabbyapi_cases(catalog) == [
        (session_pb2.HARNESS_CLAUDE, small),
        (session_pb2.HARNESS_CODEX, small),
        (session_pb2.HARNESS_CLAUDE, large),
    ]


def test_no_tabbyapi_cases_fails_instead_of_silently_skipping() -> None:
    catalog = _catalog({"HARNESS_CLAUDE": ["other/messages/example"], "HARNESS_CODEX": []})
    with pytest.raises(ValueError, match="no TabbyAPI smoke cases"):
        tabbyapi_cases(catalog)


if __name__ == "__main__":
    pytest_bazel.main()
