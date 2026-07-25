"""Filter IBKR's Swagger 2.0 spec to the read-only allowlist and emit OpenAPI 3.1.

IBKR publishes the Client Portal Web API as a single Swagger 2.0 document that
also contains order-placement routes. FastMCP's ``OpenAPIProvider`` consumes
OpenAPI 3.x, so this runs at build time (the ``ibkr_openapi_fixed`` genrule)
to produce a spec that is both:

- **read-only** — only the operations in ``route_policy.READ_ONLY_OPERATIONS``
  survive, so no trading route can be reflected into a tool; and
- **3.1** — Swagger 2.0 constructs (top-level ``type`` params, ``in: body``
  params, ``#/definitions`` refs) are transcoded to their 3.1 equivalents.

Only the schemas transitively reachable from the kept operations are carried
into ``components/schemas``; the rest of IBKR's ``definitions`` are dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from x.ibkr_mcp.route_policy import READ_ONLY_OPERATIONS, tool_spec

_DEFS_PREFIX = "#/definitions/"


def _rewrite_refs(obj: Any) -> Any:
    """Repoint ``#/definitions/X`` refs at ``#/components/schemas/X`` and drop empty enums."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str):
                out[key] = value.replace(_DEFS_PREFIX, "#/components/schemas/")
            elif key == "enum" and value == []:
                continue  # empty enums are invalid JSON Schema and FastMCP rejects them
            else:
                out[key] = _rewrite_refs(value)
        return out
    if isinstance(obj, list):
        return [_rewrite_refs(item) for item in obj]
    return obj


def _collect_definition_refs(obj: Any, acc: set[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(_DEFS_PREFIX):
                acc.add(value[len(_DEFS_PREFIX) :])
            else:
                _collect_definition_refs(value, acc)
    elif isinstance(obj, list):
        for item in obj:
            _collect_definition_refs(item, acc)


def _reachable_definitions(seeds: set[str], definitions: dict[str, Any]) -> set[str]:
    """Transitive closure of ``seeds`` over ``$ref``s within ``definitions``."""
    reached: set[str] = set()
    frontier = set(seeds)
    while frontier:
        name = frontier.pop()
        if name in reached or name not in definitions:
            continue
        reached.add(name)
        nested: set[str] = set()
        _collect_definition_refs(definitions[name], nested)
        frontier |= nested - reached
    return reached


def _convert_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Swagger 2.0 non-body parameter → OpenAPI 3.x parameter with a nested ``schema``."""
    converted: dict[str, Any] = {
        "name": param["name"],
        "in": param["in"],
        "required": bool(param.get("required", param["in"] == "path")),
    }
    if "description" in param:
        converted["description"] = param["description"]
    schema = {k: param[k] for k in ("type", "format", "items", "enum", "default", "minimum", "maximum") if k in param}
    converted["schema"] = _rewrite_refs(schema or {"type": "string"})
    return converted


def _convert_operation(method: str, path: str, op: dict[str, Any]) -> dict[str, Any]:
    converted: dict[str, Any] = {"operationId": tool_spec(method, path).name}
    if summary := op.get("summary"):
        converted["summary"] = summary
    if description := op.get("description"):
        converted["description"] = description

    parameters: list[dict[str, Any]] = []
    for param in op.get("parameters", []):
        if param.get("in") == "body":
            converted["requestBody"] = {
                "required": bool(param.get("required", False)),
                "content": {"application/json": {"schema": _rewrite_refs(param.get("schema", {"type": "object"}))}},
            }
        else:
            parameters.append(_convert_parameter(param))
    if parameters:
        converted["parameters"] = parameters

    responses: dict[str, Any] = {}
    for code, resp in op.get("responses", {}).items():
        entry: dict[str, Any] = {"description": resp.get("description", "")}
        if "schema" in resp:
            entry["content"] = {"application/json": {"schema": _rewrite_refs(resp["schema"])}}
        responses[code] = entry
    converted["responses"] = responses or {"200": {"description": "OK"}}
    return converted


def fix_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the read-only OpenAPI 3.1 spec derived from IBKR's Swagger 2.0 ``raw``."""
    raw_paths = raw.get("paths", {})
    definitions = raw.get("definitions", {})

    paths_out: dict[str, dict[str, Any]] = {}
    seed_refs: set[str] = set()
    for method, path in READ_ONLY_OPERATIONS:
        src = raw_paths.get(path)
        if src is None:
            raise ValueError(f"Allowlisted path missing from IBKR spec: {path}")
        op = src.get(method.lower())
        if op is None:
            raise ValueError(f"Allowlisted operation missing from IBKR spec: {method} {path}")
        _collect_definition_refs(op, seed_refs)
        paths_out.setdefault(path, {})[method.lower()] = _convert_operation(method, path, op)

    reachable = _reachable_definitions(seed_refs, definitions)
    schemas = {name: _rewrite_refs(definitions[name]) for name in sorted(reachable)}

    info = dict(raw.get("info", {}))
    info.setdefault("title", "IBKR Client Portal Web API")
    info["title"] = f"{info['title']} (read-only subset)"

    return {"openapi": "3.1.0", "info": info, "paths": paths_out, "components": {"schemas": schemas}}


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(json.dumps(fix_spec(json.loads(src.read_text())), indent=2))


if __name__ == "__main__":
    main()
