"""The haku-indexer worker's slice of the shared deploy-owned console config file.

The chunk role mounts the YAML the console reads but parses only the recall-index registry and the
Git CA bundle. The console-only siblings (MCP catalog, agents, policies) are deliberately unmodeled
and unvalidated here: the worker must start without them and must not fail when their vocabularies
move ahead of this image (one binary, one config — <docs/naming_and_layout.md> §5).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from haku.recall_index.config import ConfiguredRecallIndex


class IndexerConfigFile(BaseModel):
    # libgit2 does not inherit Python/OpenSSL environment variables. Configure its process-wide
    # trust store explicitly before any HTTPS recall source is cloned or fetched. Same default as
    # `ConsoleConfigFile.git_ca_bundle` — both models read the one mounted file.
    git_ca_bundle: Path = Path("/etc/ssl/certs/ca-certificates.crt")
    recall_indexes: tuple[ConfiguredRecallIndex, ...] = ()


def load_indexer_config(path: Path) -> IndexerConfigFile:
    if not path.is_file():
        raise RuntimeError(f"haku-indexer config file does not exist: {path}")
    return IndexerConfigFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
