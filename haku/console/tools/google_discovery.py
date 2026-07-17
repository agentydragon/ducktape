"""Discovery-generated Google MCP tools for haku-console.

Google API Discovery Documents — bundled in the pinned ``google-api-python-client`` wheel
(`googleapiclient/discovery_cache/documents/*.json`) — carry every method's parameters,
request/response schemas, and scopes. This module turns a `GenTool` spec (a method id plus a small
curation overlay: which params to `expose`, which constants to `pin`) into a FastMCP `Tool` whose
input schema is generated from the discovery doc and whose handler is one generic
``googleapiclient`` executor. Tools match Google's native parameter names and result shapes verbatim
— no bespoke aliasing — and the acting Operator's per-call service (`build_google_api_service`) is
bound by the in-process server builder.

The Discovery dialect is an old JSON-Schema draft plus Google extensions; the converter maps the
handful of divergences we hit (bare-name ``$ref``, ``repeated`` params, ``int64`` string-encoding,
``enumDescriptions``, per-param ``required``) and collapses ``$ref`` cycles so schemas stay finite.
It **fails loud** on an exposed param that the doc no longer defines.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, cast

from fastmcp.tools import Tool, ToolResult
from pydantic import ConfigDict, Field

_SCALAR = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
# int64/uint64 ride the wire as strings in Google JSON; keep them "string" rather than lie.
_STRING_FORMATS = {"int64", "uint64", "google-datetime", "date-time", "date", "byte", "google-duration"}


def _load_doc(api_version: str) -> dict[str, Any]:
    # Discovery docs bundled inside the pinned google-api-python-client wheel. Resolved lazily
    # (not at import) so importing this module doesn't drag googleapiclient in at test collection.
    docs = files("googleapiclient.discovery_cache.documents")
    return cast(dict[str, Any], json.loads(docs.joinpath(f"{api_version}.json").read_text()))


def _find_method(doc: dict[str, Any], method_id: str) -> dict[str, Any]:
    def walk(node: dict[str, Any]) -> dict[str, Any] | None:
        for m in (node.get("methods") or {}).values():
            if m.get("id") == method_id:
                return cast(dict[str, Any], m)
        for sub in (node.get("resources") or {}).values():
            if hit := walk(sub):
                return hit
        return None

    if (m := walk(doc)) is None:
        raise KeyError(f"{method_id} not found in {doc.get('id')}")
    return m


def _to_json_schema(
    node: dict[str, Any], schemas: dict[str, Any], seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Convert one Discovery node to JSON Schema, collapsing ``$ref`` cycles to a bare object."""
    if ref := node.get("$ref"):
        if ref in seen:
            return {"type": "object", "description": f"(recursive {ref}, elided)"}
        return _to_json_schema(schemas[ref], schemas, seen | {ref})

    out: dict[str, Any] = {}
    disc_type = node.get("type")
    if node.get("repeated") and disc_type != "array":  # Discovery marks multi-value *params* this way.
        inner = {k: v for k, v in node.items() if k not in ("repeated", "location", "required")}
        out = {"type": "array", "items": _to_json_schema(inner, schemas, seen)}
    elif disc_type == "array":
        out = {"type": "array", "items": _to_json_schema(node.get("items", {}), schemas, seen)}
    elif disc_type == "object":
        out = {"type": "object"}
        if props := node.get("properties"):
            out["properties"] = {k: _to_json_schema(v, schemas, seen) for k, v in props.items()}
        if ap := node.get("additionalProperties"):
            out["additionalProperties"] = _to_json_schema(ap, schemas, seen)
    elif node.get("format") in _STRING_FORMATS:
        out["type"] = "string"
    elif isinstance(disc_type, str) and (jt := _SCALAR.get(disc_type)):
        out["type"] = jt

    if enum := node.get("enum"):
        out["enum"] = enum
    desc = node.get("description")
    if fmt := node.get("format"):
        desc = f"{desc} (format: {fmt})" if desc else f"format: {fmt}"
    if enum_desc := node.get("enumDescriptions"):
        pairs = [f"{e}: {d}" for e, d in zip(node.get("enum") or [], enum_desc, strict=False) if d]
        if pairs:
            desc = (desc + "\n" if desc else "") + "; ".join(pairs)
    if desc:
        out["description"] = desc.strip()
    return out


@dataclass(frozen=True)
class GenTool:
    """A tier-1/2 spec: a Google discovery method plus a curation overlay."""

    method_id: str  # e.g. "gmail.users.threads.list"
    name: str  # advertised MCP tool name
    api_version: str  # e.g. "gmail.v1"
    expose: tuple[str, ...] | None = None  # param allowlist (Google names); None = all params
    pin: dict[str, str] = field(default_factory=dict)  # constants the executor injects, hidden from the agent


def _input_schema(spec: GenTool, method: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    params = method.get("parameters") or {}
    names = spec.expose if spec.expose is not None else tuple(params)
    props: dict[str, Any] = {}
    required: list[str] = []
    for n in names:
        if n in spec.pin:
            continue
        if n not in params:
            raise KeyError(f"{spec.name}: exposed param {n!r} is not in {spec.method_id} (Google changed the API?)")
        props[n] = _to_json_schema(params[n], schemas)
        if params[n].get("required"):
            required.append(n)
    # Reject unknown params, matching the hand-written tools' strict input contract.
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _output_schema(method: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any] | None:
    if response := method.get("response"):
        return _to_json_schema(response, schemas)
    return None


def _execute(service: Any, method_id: str, args: dict[str, Any]) -> Any:
    """Generic dispatch: gmail.users.threads.list -> service.users().threads().list(**args).execute().

    getattr is required, not incidental: google-api-python-client builds a Resource's methods
    dynamically at runtime from the discovery doc, so there is no static attribute to reference —
    walking the data-driven dotted method id is the whole point of a single generic executor.
    """
    _api, *path, method = method_id.split(".")
    node = service
    for segment in path:
        node = getattr(node, segment)()
    return getattr(node, method)(**args).execute()


class GeneratedGoogleTool(Tool):
    """A FastMCP tool with a discovery-generated schema and the generic googleapiclient executor."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    method_id: str
    pinned: dict[str, str] = Field(default_factory=dict)
    service: Any = None  # a googleapiclient Resource; None when built only for schema reflection

    async def run(self, arguments: dict[str, Any], **_: Any) -> ToolResult:
        merged = {**self.pinned, **arguments}
        raw = await asyncio.to_thread(_execute, self.service, self.method_id, merged)
        return self.convert_result(raw)


def build_generated_tools(specs: list[GenTool], service: Any) -> list[GeneratedGoogleTool]:
    """Build a FastMCP tool per spec, sharing one per-call ``googleapiclient`` service."""
    docs: dict[str, dict[str, Any]] = {}
    tools: list[GeneratedGoogleTool] = []
    for spec in specs:
        doc = docs.setdefault(spec.api_version, _load_doc(spec.api_version))
        method = _find_method(doc, spec.method_id)
        schemas = doc.get("schemas", {})
        tools.append(
            GeneratedGoogleTool(
                name=spec.name,
                description=(method.get("description") or "").strip(),
                parameters=_input_schema(spec, method, schemas),
                output_schema=_output_schema(method, schemas),
                method_id=spec.method_id,
                pinned=spec.pin,
                service=service,
            )
        )
    return tools
