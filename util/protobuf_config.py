"""Validate protobuf configuration and preserve its field names when rendering YAML/JSON."""

import json
from typing import Any

import protovalidate
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message


def config_dict(message: Message) -> dict[str, Any]:
    protovalidate.validate(message)
    # Protobuf map iteration order is unspecified; ConfigMap snapshots must be stable.
    rendered: dict[str, Any] = json.loads(MessageToJson(message, preserving_proto_field_name=True, sort_keys=True))
    return rendered
