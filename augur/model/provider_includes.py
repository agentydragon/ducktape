"""Resolve `{provider_config_path: <file>}` include nodes in raw provider/config YAML.

A provider sub-tree shared across many places — e.g. the macro model reused by every model
preset and by the sample-sanity fixture — should live in one file and be referenced, not
copy-pasted (the copies drift). Before Pydantic validation, a mapping that is *exactly*
`{provider_config_path: <file>}` is replaced wholesale by the YAML it points at (resolved
relative to the including file's directory); the included file may itself contain such refs.
Any other mapping/sequence is walked unchanged.

Path fields *inside* an included provider (e.g. `trained_artifact_path`) stay relative and are
anchored later against the root config's directory, so an included file and the artifacts it
names must sit in the same flat directory as the including config — which is exactly how the
deployment's ConfigMap mounts everything under `/etc/augur`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

INCLUDE_KEY = "provider_config_path"


def resolve_provider_includes(node: Any, *, base_dir: Path) -> Any:
    if isinstance(node, dict):
        if set(node) == {INCLUDE_KEY}:
            ref = Path(node[INCLUDE_KEY])
            ref_path = ref if ref.is_absolute() else base_dir / ref
            included = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
            return resolve_provider_includes(included, base_dir=ref_path.parent)
        return {key: resolve_provider_includes(value, base_dir=base_dir) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve_provider_includes(item, base_dir=base_dir) for item in node]
    return node
