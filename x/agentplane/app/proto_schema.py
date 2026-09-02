"""JSON Schema for the runner protocol's messages, derived from their descriptors.

The bridge speaks proto-JSON, and the browser's types come from the app's OpenAPI document, so the
messages the bridge carries are published there under `components.schemas` in the shape proto-JSON
gives them: camelCase field names, enums as their value names, 64-bit integers as strings, every
oneof member optional. One source, `protocol.proto`, and no second hand-written schema.
"""

from __future__ import annotations

from typing import Any

from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

_SCALARS: dict[int, dict[str, Any]] = {
    FieldDescriptor.TYPE_STRING: {"type": "string"},
    FieldDescriptor.TYPE_BOOL: {"type": "boolean"},
    FieldDescriptor.TYPE_BYTES: {"type": "string", "contentEncoding": "base64"},
    FieldDescriptor.TYPE_INT32: {"type": "integer"},
    FieldDescriptor.TYPE_UINT32: {"type": "integer"},
    FieldDescriptor.TYPE_SINT32: {"type": "integer"},
    FieldDescriptor.TYPE_FIXED32: {"type": "integer"},
    FieldDescriptor.TYPE_SFIXED32: {"type": "integer"},
    FieldDescriptor.TYPE_FLOAT: {"type": "number"},
    FieldDescriptor.TYPE_DOUBLE: {"type": "number"},
    # Proto-JSON writes 64-bit integers as decimal strings.
    FieldDescriptor.TYPE_INT64: {"type": "string", "pattern": "^-?[0-9]+$"},
    FieldDescriptor.TYPE_UINT64: {"type": "string", "pattern": "^[0-9]+$"},
    FieldDescriptor.TYPE_SINT64: {"type": "string", "pattern": "^-?[0-9]+$"},
    FieldDescriptor.TYPE_FIXED64: {"type": "string", "pattern": "^[0-9]+$"},
    FieldDescriptor.TYPE_SFIXED64: {"type": "string", "pattern": "^-?[0-9]+$"},
}

# Well-known types proto-JSON encodes as strings rather than objects.
_WELL_KNOWN: dict[str, dict[str, Any]] = {
    "google.protobuf.Timestamp": {"type": "string", "format": "date-time"},
    "google.protobuf.Duration": {"type": "string"},
}


def schemas_for(*roots: Descriptor, ref_template: str = "#/components/schemas/{model}") -> dict[str, dict[str, Any]]:
    """Schemas for `roots` and every message and enum they reach, keyed by the short name."""
    out: dict[str, dict[str, Any]] = {}
    pending = list(roots)
    while pending:
        descriptor = pending.pop()
        if descriptor.name in out or descriptor.full_name in _WELL_KNOWN:
            continue
        properties: dict[str, Any] = {}
        required: list[str] = []
        for field in descriptor.fields:
            properties[field.json_name] = _field_schema(field, ref_template, pending, out)
            # A oneof member is absent unless it is the one set; every other field proto-JSON
            # emits even at its default only when asked, so nothing is required.
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        out[descriptor.name] = schema
    return out


def _field_schema(
    field: FieldDescriptor, ref_template: str, pending: list[Descriptor], out: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        message: Descriptor = field.message_type
        if message.full_name in _WELL_KNOWN:
            item = dict(_WELL_KNOWN[message.full_name])
        else:
            pending.append(message)
            item = {"$ref": ref_template.format(model=message.name)}
    elif field.type == FieldDescriptor.TYPE_ENUM:
        enum: EnumDescriptor = field.enum_type
        out.setdefault(enum.name, {"type": "string", "enum": [value.name for value in enum.values]})
        item = {"$ref": ref_template.format(model=enum.name)}
    else:
        item = dict(_SCALARS[field.type])
    if field.is_repeated:
        return {"type": "array", "items": item}
    return item
