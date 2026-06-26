"""Export airlock Pydantic models as a unified JSON Schema to stdout.

Used by the js_json_schema Bazel macro to generate TypeScript type definitions
at build time. The generated types are consumed by both the operator frontend
and the openclaw airlock plugin.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import TypeAdapter

from airlock.models import (
    Action,
    ActionKey,
    ActionStatus,
    BackendConnectedStatus,
    BackendDegradedStatus,
    BackendStatus,
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    LogEntry,
    LogEventKind,
    OAuthProviderStatus,
)

_MODELS_TO_EXPORT: list[type[Any]] = [
    Action,
    ActionKey,
    ActionStatus,
    BackendConnectedStatus,
    BackendDegradedStatus,
    BackendStatus,
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    LogEntry,
    LogEventKind,
    OAuthProviderStatus,
]


def main() -> None:
    all_defs: dict[str, Any] = {}

    for model_class in _MODELS_TO_EXPORT:
        type_name = model_class.__name__
        schema = TypeAdapter(model_class).json_schema(mode="serialization")
        if "$defs" in schema:
            all_defs.update(schema["$defs"])
        all_defs[type_name] = {k: v for k, v in schema.items() if k != "$defs"}

    unified_schema: dict[str, Any] = {
        "type": "object",
        "title": "AirlockTypes",
        "properties": {
            name: {"$ref": f"#/$defs/{name}"} for name in all_defs if name in [m.__name__ for m in _MODELS_TO_EXPORT]
        },
        "$defs": all_defs,
    }

    json.dump(unified_schema, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
