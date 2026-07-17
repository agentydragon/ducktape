"""Spike: generate MCP tool schemas from Google API Discovery Documents.

Pure stdlib. Loads a vendored discovery doc, resolves a chosen method's parameters +
(for writes) its request-body schema, and emits an MCP-style tool
`{name, description, inputSchema, ...}` — no hand-written schema.

Goal of the spike: see whether the generated `inputSchema` is (a) clean enough to hand an
LLM as an MCP tool and (b) a sane basis for the haku-console Google servers. Run:
`python3 discovery_to_mcp.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

DOCS = Path(__file__).parent / "discovery_docs"

# Discovery scalar type -> JSON Schema type. `any` stays untyped.
_SCALAR = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}

# int64/uint64 ride the wire as strings in Google JSON; note it rather than lie with "integer".
_STRING_FORMATS = {"int64", "uint64", "google-datetime", "date-time", "date", "byte", "google-duration"}


def load(api_version: str) -> dict:
    return json.loads((DOCS / f"{api_version}.json").read_text())


def find_method(doc: dict, method_id: str) -> dict:
    """Locate a method by its dotted `id` (e.g. drive.files.list) anywhere in the resource tree."""

    def walk(node: dict) -> dict | None:
        for m in (node.get("methods") or {}).values():
            if m.get("id") == method_id:
                return m
        for sub in (node.get("resources") or {}).values():
            if hit := walk(sub):
                return hit
        return None

    if (m := walk(doc)) is None:
        raise KeyError(f"{method_id} not found in {doc.get('id')}")
    return m


def to_json_schema(node: dict, schemas: dict, seen: frozenset[str] = frozenset()) -> dict:
    """Convert one Discovery parameter/property/schema node to JSON Schema.

    `seen` guards $ref cycles (e.g. Drive `File` nests itself); a repeat collapses to a bare
    object so the schema stays finite and self-contained.
    """
    if ref := node.get("$ref"):
        if ref in seen:
            return {"type": "object", "description": f"(recursive {ref}, elided)"}
        return to_json_schema(schemas[ref], schemas, seen | {ref})

    out: dict = {}
    disc_type = node.get("type")

    # Discovery marks repeated *parameters* with `repeated: true` instead of type=array.
    if node.get("repeated") and disc_type != "array":
        inner = {k: v for k, v in node.items() if k not in ("repeated", "location", "required")}
        out = {"type": "array", "items": to_json_schema(inner, schemas, seen)}
    elif disc_type == "array":
        out = {"type": "array", "items": to_json_schema(node.get("items", {}), schemas, seen)}
    elif disc_type == "object":
        out = {"type": "object"}
        if props := node.get("properties"):
            out["properties"] = {k: to_json_schema(v, schemas, seen) for k, v in props.items()}
        if ap := node.get("additionalProperties"):
            out["additionalProperties"] = to_json_schema(ap, schemas, seen)
    elif node.get("format") in _STRING_FORMATS:
        out["type"] = "string"
    elif jt := _SCALAR.get(disc_type):
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


def method_to_tool(doc: dict, method_id: str) -> dict:
    method = find_method(doc, method_id)
    schemas = doc.get("schemas", {})
    props: dict = {}
    required: list[str] = []

    for pname, p in (method.get("parameters") or {}).items():
        props[pname] = to_json_schema(p, schemas)
        if p.get("required"):
            required.append(pname)

    if request := method.get("request"):
        props["body"] = to_json_schema(request, schemas)
        required.append("body")

    input_schema: dict = {"type": "object", "properties": props}
    if required:
        input_schema["required"] = required

    return {
        "name": method_id.replace(".", "_"),
        "description": (method.get("description") or "").strip(),
        "read_only": method.get("httpMethod") == "GET",
        "scopes": method.get("scopes", []),
        "inputSchema": input_schema,
    }


# Curated slice of the haku-console Google surface (the P1-P3 read tools + two writes for contrast).
ALLOWLIST = [
    ("drive.v3", "drive.files.list", "P1"),
    ("drive.v3", "drive.files.get", "P1"),
    ("tasks.v1", "tasks.tasklists.list", "P1"),
    ("tasks.v1", "tasks.tasks.list", "P1"),
    ("gmail.v1", "gmail.users.messages.list", "P1"),
    ("calendar.v3", "calendar.events.list", "P1"),
    ("drive.v3", "drive.comments.list", "P2"),
    ("drive.v3", "drive.files.export", "P2"),
    ("docs.v1", "docs.documents.get", "P2"),
    ("sheets.v4", "sheets.spreadsheets.values.get", "P2"),
    ("calendar.v3", "calendar.events.insert", "WRITE (contrast)"),
    ("gmail.v1", "gmail.users.drafts.create", "WRITE (contrast)"),
]


def main() -> None:
    out_dir = Path(__file__).parent / "generated"
    out_dir.mkdir(exist_ok=True)
    docs = {av: load(av) for av in {av for av, _, _ in ALLOWLIST}}

    print(f"{'tier':<18}{'tool':<34}{'kind':<7}{'#props':<7}{'schema chars'}")
    print("-" * 78)
    for av, mid, tier in ALLOWLIST:
        tool = method_to_tool(docs[av], mid)
        (out_dir / f"{tool['name']}.json").write_text(json.dumps(tool, indent=2))
        blob = json.dumps(tool["inputSchema"])
        kind = "read" if tool["read_only"] else "write"
        nprops = len(tool["inputSchema"]["properties"])
        print(f"{tier:<18}{tool['name']:<34}{kind:<7}{nprops:<7}{len(blob)}")

    print(f"\nFull tool JSON written to {out_dir}/")


if __name__ == "__main__":
    main()
