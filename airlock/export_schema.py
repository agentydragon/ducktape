"""Export Airlock API models as a unified JSON Schema to stdout."""

from __future__ import annotations

import json
import sys

from pydantic.json_schema import models_json_schema

from airlock.models import (
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthProviderStatus,
)

_MODELS_TO_EXPORT = (
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthProviderStatus,
)


def main() -> None:
    model_schemas, definitions = models_json_schema([(model, "serialization") for model in _MODELS_TO_EXPORT])
    unified_schema = {
        "type": "object",
        "title": "AirlockTypes",
        "properties": {model.__name__: schema for (model, _), schema in model_schemas.items()},
        **definitions,
    }

    json.dump(unified_schema, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
