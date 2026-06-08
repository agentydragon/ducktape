from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def stable_identity_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_json_stable(payload), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def _json_stable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_stable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_stable(inner) for key, inner in value.items()}
    if isinstance(value, tuple | list):
        return [_json_stable(inner) for inner in value]
    return value
