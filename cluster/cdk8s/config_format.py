"""Serialize configuration models and source mappings at the ConfigMap text boundary.

Unset model fields are excluded. cdk8s YAML formatting omits null mapping values;
JSON preserves them and uses Pydantic's encoder for model-specific JSON policies.
"""

from __future__ import annotations

from typing import Any

from cdk8s import Yaml
from pydantic import BaseModel, TypeAdapter

_MAPPING = TypeAdapter[dict[str, Any]](dict[str, Any])


def yaml_config(data: BaseModel | dict[str, Any], *, exclude_none: bool = False) -> str:
    if isinstance(data, BaseModel):
        rendered = data.model_dump(mode="json", exclude_unset=True, exclude_none=exclude_none)
    else:
        rendered = _MAPPING.dump_python(data, mode="json", exclude_unset=True, exclude_none=exclude_none)
    return Yaml.format_objects([rendered])


def json_config(data: BaseModel | dict[str, Any]) -> str:
    if isinstance(data, BaseModel):
        rendered = data.model_dump_json(indent=2, ensure_ascii=True, exclude_unset=True)
    else:
        rendered = _MAPPING.dump_json(data, indent=2, ensure_ascii=True, exclude_unset=True).decode()
    return rendered + "\n"
