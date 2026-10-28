"""Deterministic protobuf-to-dictionary serialization with schema field names."""

import json
from typing import Any

from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message


def message_dict(message: Message) -> dict[str, Any]:
    """Serialize present fields; source composition and validation belong to the caller."""
    # Protobuf map iteration order is unspecified; ConfigMap snapshots must be stable.
    rendered: dict[str, Any] = json.loads(MessageToJson(message, preserving_proto_field_name=True, sort_keys=True))
    return rendered
