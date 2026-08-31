"""The haku-indexer worker's config model: the recall-index registry and the Git CA bundle.

Each chunk pod mounts its own per-index slice of the deploy-owned registry, derivation-tested
against the `recall_indexes` the console reads, so a slice carries exactly these keys. The
console-only vocabularies (MCP catalog, agents, policies) stay deliberately unmodeled and
unvalidated here: the worker must not fail when they move ahead of this image (one binary, one
config — <docs/naming_and_layout.md> §5).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from haku.recall_index.config import ConfiguredRecallIndex


class IndexerConfigFile(BaseModel):
    # libgit2 does not inherit Python/OpenSSL environment variables. Configure its process-wide
    # trust store explicitly before any HTTPS recall source is cloned or fetched. Same default as
    # `ConsoleConfigFile.git_ca_bundle`; the slice derivation keeps the deployed values equal.
    git_ca_bundle: Path = Path("/etc/ssl/certs/ca-certificates.crt")
    recall_indexes: dict[str, ConfiguredRecallIndex] = Field(default_factory=dict)
