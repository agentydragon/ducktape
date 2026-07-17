"""Unit tests for the discovery-driven tool factory (google_discovery.py).

Test posture for generated Google tools: the **factory is the logic**, so it gets deep unit tests
here — Discovery→JSON-Schema conversion, the expose/pin overlay, output schema, `$ref`-cycle
collapse, fail-loud, and generic dispatch — driven by a small **synthetic** discovery doc, not a
real Google one. That keeps these tests about *our* conversion (not Google's data) and stops them
drifting when the wheel snapshot updates. Each generated **server** (gmail, and future Drive/Tasks)
then needs only a thin smoke test: its advertised tool-name set + one representative round-trip +
one strict-input rejection (see `test_gmail.py`). Don't re-test the factory once per generated tool.
"""

from typing import Any

import pytest
import pytest_bazel

from haku.console.tools import google_discovery
from haku.console.tools.google_discovery import GenTool, _execute, build_generated_tools

# A synthetic discovery doc exercising each dialect feature the converter must handle.
_DOC: dict[str, Any] = {
    "id": "fake:v1",
    "schemas": {
        "Widget": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The id."},
                "size": {"type": "integer", "format": "int64", "description": "Byte size."},
                "child": {"$ref": "Widget"},  # self-reference -> cycle
            },
        },
        "WidgetList": {
            "type": "object",
            "properties": {
                "widgets": {"type": "array", "items": {"$ref": "Widget"}},
                "nextPageToken": {"type": "string"},
            },
        },
    },
    "resources": {
        "widgets": {
            "methods": {
                "list": {
                    "id": "fake.widgets.list",
                    "httpMethod": "GET",
                    "parameters": {
                        "userId": {"type": "string", "required": True, "location": "path"},
                        "q": {"type": "string", "description": "Query."},
                        "maxResults": {"type": "integer", "format": "uint32"},
                        "since": {"type": "integer", "format": "int64", "description": "Epoch ms."},
                        "labelIds": {"type": "string", "repeated": True, "description": "Labels."},
                        "order": {"type": "string", "enum": ["a", "b"], "enumDescriptions": ["Alpha", "Beta"]},
                    },
                    "response": {"$ref": "WidgetList"},
                    "scopes": ["https://example.com/auth/read"],
                },
                "get": {
                    "id": "fake.widgets.get",
                    "httpMethod": "GET",
                    "parameters": {
                        "userId": {"type": "string", "required": True},
                        "id": {"type": "string", "required": True},
                    },
                    "response": {"$ref": "Widget"},
                },
            }
        },
        "sub": {
            "resources": {
                "items": {
                    "methods": {
                        "list": {
                            "id": "fake.sub.items.list",
                            "httpMethod": "GET",
                            "parameters": {"userId": {"type": "string", "required": True}},
                        }
                    }
                }
            }
        },
    },
}


@pytest.fixture(autouse=True)
def _synthetic_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_discovery, "_load_doc", lambda _api_version: _DOC)


class _FakeService:
    """Records the chained call; every attribute is a chainable method, `.execute()` returns result."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)

        def call(**kwargs: Any) -> "_FakeService":
            self.calls.append((name, kwargs))
            return self

        return call

    def execute(self) -> Any:
        return self.result


def _build(spec: GenTool, service: Any = None):
    return build_generated_tools([spec], service)[0]


def test_input_schema_maps_dialect_and_applies_overlay() -> None:
    tool = _build(GenTool("fake.widgets.list", "list", "fake.v1", pin={"userId": "me"}))
    props = tool.parameters["properties"]
    assert "userId" not in props  # pinned constant is never exposed
    assert props["q"] == {"type": "string", "description": "Query."}
    assert props["maxResults"]["type"] == "integer"  # uint32 stays integer
    assert props["since"]["type"] == "string"  # int64 rides the wire as a string
    assert props["labelIds"] == {  # repeated param -> array of the base type (desc rides both levels)
        "type": "array",
        "items": {"type": "string", "description": "Labels."},
        "description": "Labels.",
    }
    assert props["order"]["enum"] == ["a", "b"]
    assert "Alpha" in props["order"]["description"]  # enumDescriptions folded in
    assert tool.parameters["additionalProperties"] is False  # strict-input contract
    assert "required" not in tool.parameters  # userId was required but pinned -> dropped


def test_expose_allowlist_restricts_params() -> None:
    tool = _build(GenTool("fake.widgets.list", "list", "fake.v1", expose=("q",), pin={"userId": "me"}))
    assert set(tool.parameters["properties"]) == {"q"}


def test_required_is_derived_from_non_pinned_params() -> None:
    tool = _build(GenTool("fake.widgets.get", "get", "fake.v1", pin={"userId": "me"}))
    assert tool.parameters["required"] == ["id"]


def test_output_schema_from_response_ref_with_cycle_collapsed() -> None:
    tool = _build(GenTool("fake.widgets.get", "get", "fake.v1", pin={"userId": "me"}))
    assert tool.output_schema is not None
    child = tool.output_schema["properties"]["child"]  # self-ref collapsed to a bare object
    assert child["type"] == "object"
    assert "recursive" in child["description"]


def test_no_output_schema_when_method_has_no_response() -> None:
    tool = _build(GenTool("fake.sub.items.list", "items_list", "fake.v1", pin={"userId": "me"}))
    assert tool.output_schema is None


def test_unknown_exposed_param_fails_loud() -> None:
    with pytest.raises(KeyError, match="nope"):
        _build(GenTool("fake.widgets.list", "list", "fake.v1", expose=("nope",)))


def test_unknown_method_id_fails_loud() -> None:
    with pytest.raises(KeyError, match="missing"):
        _build(GenTool("fake.widgets.missing", "x", "fake.v1"))


def test_executor_walks_dotted_path_and_merges_pinned_constants() -> None:
    service = _FakeService(result={"ok": True})
    assert _execute(service, "fake.widgets.get", {"userId": "me", "id": "w1"}) == {"ok": True}
    assert service.calls == [("widgets", {}), ("get", {"userId": "me", "id": "w1"})]


def test_executor_walks_nested_resource_path() -> None:
    service = _FakeService()
    _execute(service, "fake.sub.items.list", {"userId": "me"})
    assert service.calls == [("sub", {}), ("items", {}), ("list", {"userId": "me"})]


if __name__ == "__main__":
    pytest_bazel.main()
