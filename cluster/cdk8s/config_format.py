"""Serialize configuration models and source mappings at the ConfigMap text boundary.

Unset model fields are excluded. cdk8s YAML formatting omits null mapping values;
JSON preserves them.
"""

from __future__ import annotations

import json
from typing import Any

from cdk8s import Yaml
from pydantic import BaseModel, TypeAdapter

_CONFIG = TypeAdapter[Any](Any)


def yaml_config(data: BaseModel | dict[str, Any], *, exclude_none: bool = False) -> str:
    return Yaml.format_objects([_CONFIG.dump_python(data, mode="json", exclude_unset=True, exclude_none=exclude_none)])


def json_config(data: BaseModel | dict[str, Any]) -> str:
    return json.dumps(_CONFIG.dump_python(data, mode="json", exclude_unset=True), indent=2) + "\n"
