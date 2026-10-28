"""Serialize configuration models and source mappings at the ConfigMap text boundary.

Only explicitly set model fields are emitted. Valid JSON is valid JSON5, so plain
`json.dumps` output needs no JSON5-specific escaping.
"""

from __future__ import annotations

import json
from typing import Any

from cdk8s import Yaml
from pydantic import BaseModel, TypeAdapter

_CONFIG = TypeAdapter[Any](Any)


def yaml_config(data: BaseModel | dict[str, Any], *, exclude_none: bool = False) -> str:
    return Yaml.format_objects(
        [_CONFIG.dump_python(data, mode="json", by_alias=False, exclude_unset=True, exclude_none=exclude_none)]
    )


def json5_config(data: BaseModel | dict[str, Any]) -> str:
    return json.dumps(_CONFIG.dump_python(data, mode="json", by_alias=False, exclude_unset=True), indent=2) + "\n"
