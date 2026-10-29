"""Export Airlock API models as a unified JSON Schema to stdout."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import TypeAdapter

from airlock.models import (
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthProviderStatus,
)
from util.json_schema import register_models

_MODELS_TO_EXPORT: list[type[Any]] = [
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthProviderStatus,
]


def main() -> None:
    all_defs: dict[str, Any] = {}
    register_models(
        all_defs,
        [(model_class.__name__, "serialization", TypeAdapter(model_class)) for model_class in _MODELS_TO_EXPORT],
        ref_template="#/$defs/{model}",
    )
    unified_schema: dict[str, Any] = {
        "type": "object",
        "title": "AirlockTypes",
        "properties": {m.__name__: {"$ref": f"#/$defs/{m.__name__}"} for m in _MODELS_TO_EXPORT},
        "$defs": all_defs,
    }
    json.dump(unified_schema, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
