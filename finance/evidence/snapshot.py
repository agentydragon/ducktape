"""Capture the exact source bytes used by one evidence-consuming operation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pygit2

from finance.evidence.sources import EvidenceSource


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Source bytes captured before an operation; a Git checkout is pinned to one commit.

    Git-backed input must match its checked-in blobs. Refusing modified input prevents
    attributing uncommitted bytes to a commit. A plain directory is supported for one-shot
    downloads and fixtures; its per-file hashes identify the captured bytes, but do not claim
    that separately written upstream files were observed atomically.
    """

    revision: str | None
    _bytes: Mapping[EvidenceSource, bytes]

    @classmethod
    def capture(cls, directory: Path, sources: Iterable[EvidenceSource]) -> EvidenceSnapshot:
        root = directory.resolve(strict=True)
        requested = tuple(sources)
        if len({source.provenance_label for source in requested}) != len(requested):
            raise ValueError("evidence sources must have unique provenance labels")
        repo = pygit2.Repository(str(root)) if (root / ".git").exists() else None
        commit = repo.head.peel(pygit2.Commit) if repo is not None else None
        captured: dict[EvidenceSource, bytes] = {}
        for source in requested:
            path = root / source.output_filename
            if not path.is_file():
                raise RuntimeError(f"evidence not found in checkout: {path}")
            body = path.read_bytes()
            if commit is not None and repo is not None:
                try:
                    blob = repo[commit.tree[source.output_filename].id]
                except KeyError as exc:
                    raise ValueError(
                        f"{source.provenance_label} is not tracked in evidence revision {commit.id}"
                    ) from exc
                if not isinstance(blob, pygit2.Blob) or blob.data != body:
                    raise ValueError(f"{source.provenance_label} differs from evidence revision {commit.id}")
            captured[source] = body
        return cls(revision=str(commit.id) if commit is not None else None, _bytes=MappingProxyType(captured))

    def source_bytes(self, source: EvidenceSource) -> bytes:
        """Return captured bytes; undeclared sources cannot enter the operation later."""
        return self._bytes[source]

    def source_hashes(self) -> dict[str, str]:
        """SHA-256 of the exact captured bytes, keyed by stable source identity."""
        return {source.provenance_label: hashlib.sha256(body).hexdigest() for source, body in self._bytes.items()}
