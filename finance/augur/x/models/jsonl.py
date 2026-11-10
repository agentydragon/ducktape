"""Write JSONL (one JSON object per line) — a tiny shared test helper for round-tripping
JSONL artifacts (price observations, PE trajectories) through the readers under test."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> Path:
    """Write `rows` as JSONL to `path` (one JSON object per line; empty when there are no
    rows, never a stray blank line) and return it."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
