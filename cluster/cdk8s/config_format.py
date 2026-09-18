"""Formats a Python dict as text for a ConfigMap value: YAML or JSON5 (valid JSON is
valid JSON5, so plain `json.dumps` output needs no JSON5-specific escaping).
"""

from __future__ import annotations

import json

from cdk8s import Yaml


def yaml_config(data: dict) -> str:
    return Yaml.format_objects([data])


def json5_config(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"
