from __future__ import annotations

from typing import Any

import pytest_bazel

from haku.console.export_schema import FOLLOW_MESSAGE_SCHEMA, console_openapi_document


def schemas() -> dict[str, Any]:
    return dict(console_openapi_document()["components"]["schemas"])


def test_the_follow_socket_messages_are_documented() -> None:
    # A WebSocket carries no route, so without this the browser's types for what it receives would
    # be written by hand and a renamed field would break nothing until it reached a tab.
    published = schemas()[FOLLOW_MESSAGE_SCHEMA]
    assert {str(branch["$ref"]).rsplit("/", 1)[-1] for branch in published["oneOf"]} == {
        "ConversationSnapshot",
        "ConversationUpdate",
    }
    assert published["discriminator"]["propertyName"] == "message_type"


def test_a_message_carries_the_components_a_read_returns() -> None:
    # The same components, not a second description of them: one rename moves both surfaces, and a
    # follower and a reader cannot come to disagree about what a conversation looks like.
    published = schemas()
    assert published["ConversationSnapshot"]["properties"]["conversation"] == {
        "$ref": "#/components/schemas/ConversationView"
    }
    for surface in ("ConversationUpdate", "ConversationView"):
        assert published[surface]["properties"]["entries"]["items"] == {
            "$ref": "#/components/schemas/ConversationEntry"
        }
    union = published["ConversationEntry"]
    assert all(str(branch["$ref"]).startswith("#/components/schemas/") for branch in union["oneOf"])
    assert union["discriminator"]["propertyName"] == "kind"


def test_runtime_and_harness_kind_are_read_only_closed_identity_fields() -> None:
    # runtime_kind→harness_kind wire rename (naming_and_layout.md §3.1, #4772): readers are on
    # `harness_kind`; every identity-bearing model keeps publishing both names, each a required
    # reference to the same closed enum, until the contract step drops `runtime_kind` after the
    # reader-switch image rolls out.
    published = schemas()
    runtime_ref = {"$ref": "#/components/schemas/RuntimeKind"}
    assert published["RuntimeKind"]["enum"] == ["claude_code", "codex_app_server"]
    for model in ("ConversationSummary", "ConversationView", "SessionFramePage", "SessionProvisioningView"):
        properties = published[model]["properties"]
        required = published[model]["required"]
        for field_name in ("runtime_kind", "harness_kind"):
            field = properties[field_name]
            # Pydantic wraps a referenced enum in `allOf` when the field also carries a description.
            assert field.get("$ref") == runtime_ref["$ref"] or field.get("allOf") == [runtime_ref]
            assert field_name in required


def test_tool_call_serializer_preserves_the_structured_frontend_schema() -> None:
    published = schemas()["ToolCallRecord"]
    assert {"tool_call_id", "caller", "status", "arguments", "rationale", "result"} <= published["properties"].keys()
    assert {"tool_call_id", "caller", "status", "arguments"} <= set(published["required"])


if __name__ == "__main__":
    pytest_bazel.main()
