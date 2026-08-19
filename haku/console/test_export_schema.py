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
    assert published["ConversationUpdate"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/ConversationItemView"
    }


if __name__ == "__main__":
    pytest_bazel.main()
