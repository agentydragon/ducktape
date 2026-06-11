"""Generate JSON schema from Pydantic models for TypeScript type generation.

Outputs a unified JSON schema consumed by json2ts (via js_run_binary in BUILD.bazel)
to produce TypeScript type declarations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from agent_core.events import ToolCall
from x.agent_server.approvals import ApprovalRequest
from x.agent_server.mcp_bridge.agents import AgentInfo
from x.agent_server.persist.types import ApprovalOutcome, EventType
from x.agent_server.server.protocol import AgentStatus

_MODELS_TO_EXPORT = [ToolCall, ApprovalOutcome, AgentStatus, EventType, ApprovalRequest, AgentInfo]


def _build_unified_schema() -> dict[str, Any]:
    all_defs: dict[str, Any] = {}
    for model_class in _MODELS_TO_EXPORT:
        schema = TypeAdapter(model_class).json_schema(mode="serialization")
        if "$defs" in schema:
            all_defs.update(schema["$defs"])
        all_defs[model_class.__name__] = {k: v for k, v in schema.items() if k != "$defs"}

    export_names = {m.__name__ for m in _MODELS_TO_EXPORT}
    return {
        "type": "object",
        "title": "AgentTypes",
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in all_defs if name in export_names},
        "$defs": all_defs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate JSON schema from Pydantic models")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(_build_unified_schema(), indent=2))


if __name__ == "__main__":
    main()
