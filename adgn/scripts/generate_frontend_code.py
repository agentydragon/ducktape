#!/usr/bin/env python3
"""Generate TypeScript code from Python sources.

This script performs two code generation tasks:
1. Extracts MCP resource URI constants from Python and generates TypeScript helpers
2. Extracts Pydantic models and generates TypeScript type definitions

Run with: python scripts/generate_frontend_code.py
Or via npm: npm run codegen
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from pydantic import TypeAdapter

# Import models to export
from adgn.agent.approvals import ApprovalRequest
from adgn.agent.mcp_bridge.servers.agents import (
    AbortAgentArgs,
    AgentApprovalsHistory,
    AgentApprovalsPending,
    AgentInfo,
    AgentList,
    AgentPolicyProposals,
    ApprovalHistoryEntry,
    ApproveToolCallArgs,
    PendingApproval,
    PolicyProposalInfo,
    RejectToolCallArgs,
)
from adgn.agent.persist import ApprovalOutcome, Decision, EventType, PersistenceRunStatus, ToolCallExecution, ToolCallRecord
from adgn.agent.types import ToolCall


# ============================================================================
# MCP Constants Generation
# ============================================================================


def extract_constants_from_file(python_file: Path) -> dict[str, Any]:
    """Extract Final[str] constants from a Python file using runtime evaluation."""
    namespace: dict[str, Any] = {}

    try:
        with open(python_file) as f:
            code = f.read()
        exec(code, namespace)
    except Exception as e:
        print(f"Error executing constants file: {e}", file=sys.stderr)
        raise

    constants: dict[str, Any] = {}

    # Extract all string constants that contain 'URI'
    for name, value in namespace.items():
        if isinstance(value, str) and "URI" in name and not name.startswith("_"):
            constants[name] = value

    return constants


def classify_constants(
    constants: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Classify constants into simple URIs and format strings."""
    simple_uris: list[tuple[str, str]] = []
    format_uris: list[tuple[str, str]] = []

    for name, value in sorted(constants.items()):
        if isinstance(value, str):
            if "{" in value and "}" in value:
                format_uris.append((name, value))
            else:
                simple_uris.append((name, value))

    return simple_uris, format_uris


def generate_mcp_constants_typescript(
    simple_uris: list[tuple[str, str]], format_uris: list[tuple[str, str]]
) -> str:
    """Generate TypeScript constants and helpers."""
    output: list[str] = []

    output.append("// Auto-generated MCP resource URI constants")
    output.append("// Do not edit manually - regenerate with: npm run codegen")
    output.append("")

    # Simple URI constants
    if simple_uris:
        output.append("/** Simple resource URI constants */")
        output.append("export const MCPUris = {")
        for py_name, uri in simple_uris:
            ts_name = _to_camel_case(py_name)
            output.append(f"  {ts_name}: '{uri}',")
        output.append("} as const")
        output.append("")

    # Helper functions for format strings
    if format_uris:
        output.append("/** Helper functions for resource URI format strings */")
        for py_name, format_str in format_uris:
            ts_func_name = _to_camel_case(py_name.replace("_FMT", ""))
            params = _extract_format_params(format_str)

            if params:
                param_defs = ", ".join([f"{p}: string" for p in params])
                output.append(f"export function {ts_func_name}({param_defs}): string {{")
                output.append(f"  return `{_escape_format_string(format_str)}`")
                output.append("}")
            else:
                ts_name = _to_camel_case(py_name)
                output.append(f"export const {ts_name} = '{format_str}' as const")

        output.append("")

    return "\n".join(output)


def _to_camel_case(snake_str: str) -> str:
    """Convert UPPER_SNAKE_CASE to camelCase."""
    components = snake_str.split("_")
    return components[0].lower() + "".join(x.capitalize() for x in components[1:])


def _extract_format_params(format_str: str) -> list[str]:
    """Extract parameter names from a format string like 'resource://foo/{bar}'."""
    matches = re.findall(r"\{(\w+)\}", format_str)
    return list(dict.fromkeys(matches))  # Remove duplicates while preserving order


def _escape_format_string(format_str: str) -> str:
    """Escape a format string for use in template literals."""
    return re.sub(r"\{(\w+)\}", r"${\1}", format_str)


def generate_mcp_constants() -> None:
    """Generate TypeScript MCP constants from Python."""
    project_root = Path(__file__).parent.parent
    constants_file = project_root / "src" / "adgn" / "mcp" / "_shared" / "constants.py"

    if not constants_file.exists():
        print(f"Error: Constants file not found at {constants_file}", file=sys.stderr)
        sys.exit(1)

    output_dir = project_root / "src" / "adgn" / "agent" / "web" / "src" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "mcpConstants.ts"

    print(f"[1/2] Generating MCP constants from {constants_file}...")

    constants = extract_constants_from_file(constants_file)

    if not constants:
        print("  Warning: No URI constants found in Python file", file=sys.stderr)
        return

    print(f"  Found {len(constants)} URI constants")

    simple_uris, format_uris = classify_constants(constants)

    print(f"  Simple URIs: {len(simple_uris)}")
    print(f"  Format strings: {len(format_uris)}")

    ts_code = generate_mcp_constants_typescript(simple_uris, format_uris)

    output_file.write_text(ts_code)
    print(f"  ✓ Generated {output_file}")


# ============================================================================
# TypeScript Types Generation
# ============================================================================


def generate_typescript_from_schema(schema: dict[str, Any], type_name: str) -> str:
    """Generate TypeScript interface from JSON Schema using json-schema-to-typescript."""
    schema_json = json.dumps(schema, indent=2)

    try:
        result = subprocess.run(
            ["npx", "json-schema-to-typescript", "--stdin", "--bannerComment", ""],
            input=schema_json,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"  Error generating TypeScript for {type_name}:", file=sys.stderr)
        print(e.stderr, file=sys.stderr)
        raise


def generate_pydantic_types() -> None:
    """Generate TypeScript types from Pydantic models."""
    models_to_export = [
        # Core types
        ToolCall,
        # Enums
        ApprovalOutcome,
        PersistenceRunStatus,
        EventType,
        # Decision and execution
        Decision,
        ToolCallExecution,
        ToolCallRecord,
        # Approval types
        ApprovalRequest,
        PendingApproval,
        ApprovalHistoryEntry,
        # Agent info
        AgentInfo,
        AgentList,
        AgentApprovalsPending,
        AgentApprovalsHistory,
        PolicyProposalInfo,
        AgentPolicyProposals,
        # Tool args
        ApproveToolCallArgs,
        RejectToolCallArgs,
        AbortAgentArgs,
    ]

    project_root = Path(__file__).parent.parent
    output_dir = project_root / "src" / "adgn" / "agent" / "web" / "src" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "types.ts"

    print(f"[2/2] Generating TypeScript types to {output_file}...")

    all_defs: dict[str, Any] = {}

    for model_class in models_to_export:
        type_name = model_class.__name__
        print(f"  Processing {type_name}...")
        schema = TypeAdapter(model_class).json_schema(mode="serialization")

        if "$defs" in schema:
            all_defs.update(schema["$defs"])

        all_defs[type_name] = {k: v for k, v in schema.items() if k != "$defs"}

    # Create unified schema
    unified_schema = {
        "type": "object",
        "title": "AgentTypes",
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in all_defs if name in [m.__name__ for m in models_to_export]},
        "$defs": all_defs,
    }

    try:
        ts_code = generate_typescript_from_schema(unified_schema, "AgentTypes")
        ts_output = [
            "// Auto-generated TypeScript types from Pydantic models",
            "// Do not edit manually - regenerate with: npm run codegen",
            "",
            ts_code.strip(),
        ]

        output_file.write_text("\n".join(ts_output))
        print(f"  ✓ Generated TypeScript types for {len(models_to_export)} models")
    except Exception as e:
        print(f"  Error generating TypeScript: {e}", file=sys.stderr)
        raise


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    """Run all code generation tasks."""
    print("=== Frontend Code Generation ===")
    print()

    try:
        generate_mcp_constants()
        print()
        generate_pydantic_types()
        print()
        print("✓ All code generation completed successfully")
    except Exception as e:
        print(f"\n✗ Code generation failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
