"""Typed streaming readers for the Manifold Markets full-site dump (2024-07-06 snapshot).

The dump (https://docs.manifold.markets/data) is three zips — contracts,
comments, bets — each containing a single member that is one large JSON array
(the bets array is ~6.8 GB uncompressed). Records are decoded incrementally
straight from the compressed stream; nothing is extracted to disk or held in
memory beyond one record plus a read buffer.

Models keep only the fields the gym harvest needs and use snake_case names
aliased to the dump's camelCase keys. Timestamps stay epoch-milliseconds as in
the dump; convert with `ms_to_datetime`.

License: the dump is personal/academic/non-commercial use only (commercial use
requires a license from data@manifold.markets); see the mirror's README at
s3://loom-gym/harvest/raw/manifold-20240706/README.md.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from more_itertools import one
from pydantic import BaseModel, ConfigDict, Field

_CHUNK_SIZE = 8 << 20


def ms_to_datetime(ms: int) -> datetime:
    """Convert a dump epoch-milliseconds timestamp to an aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def tiptap_plain_text(node: object) -> str:
    """Flatten a TipTap JSON document to plain text (paragraph/heading breaks become newlines)."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []
    if node.get("type") == "text":
        parts.append(str(node.get("text", "")))
    content = node.get("content")
    if isinstance(content, list):
        parts.extend(tiptap_plain_text(child) for child in content)
    if node.get("type") in ("paragraph", "heading"):
        parts.append("\n")
    return "".join(parts)


class ContractRecord(BaseModel):
    """One market. `resolution`/`resolution_time` are absent for unresolved markets."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    question: str
    outcome_type: str = Field(alias="outcomeType")
    created_time: int = Field(alias="createdTime", description="Epoch ms.")
    resolution: str | None = Field(default=None, description='For binary markets: "YES"/"NO"/"CANCEL"/"MKT".')
    resolution_time: int | None = Field(default=None, alias="resolutionTime", description="Epoch ms.")
    unique_bettor_count: int = Field(alias="uniqueBettorCount")
    description: str | dict[str, object] = Field(
        description="Plain string on pre-2022 markets, TipTap JSON document otherwise."
    )

    def description_text(self) -> str:
        if isinstance(self.description, str):
            return self.description
        return tiptap_plain_text(self.description)


class CommentRecord(BaseModel):
    """One comment; the body is either plain `text` (old) or a TipTap `content` document (new)."""

    model_config = ConfigDict(populate_by_name=True)

    contract_id: str = Field(alias="contractId")
    created_time: int = Field(alias="createdTime", description="Epoch ms.")
    user_name: str = Field(alias="userName")
    text: str | None = None
    content: dict[str, object] | None = None

    def body_text(self) -> str:
        if self.text is not None:
            return self.text
        return tiptap_plain_text(self.content)


class BetRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_id: str = Field(alias="contractId")
    created_time: int = Field(alias="createdTime", description="Epoch ms.")
    prob_after: float = Field(alias="probAfter")


def iter_json_array(reader: IO[str], chunk_size: int = _CHUNK_SIZE) -> Iterator[object]:
    """Incrementally yield elements of a JSON array read from `reader`.

    Raises `json.JSONDecodeError` on malformed input and `ValueError` on
    truncation (EOF before the closing bracket).
    """
    decoder = json.JSONDecoder()
    buf = reader.read(chunk_size)
    head = buf.lstrip()
    if not head.startswith("["):
        raise ValueError(f"not a JSON array: starts with {head[:20]!r}")
    pos = len(buf) - len(head) + 1
    while True:
        # Skip inter-element whitespace and commas, refilling the buffer at its end.
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf):
                break
            buf = reader.read(chunk_size)
            if not buf:
                raise ValueError("truncated JSON array: EOF before closing bracket")
            pos = 0
        if buf[pos] == "]":
            return
        try:
            element, pos = decoder.raw_decode(buf, pos)
        except json.JSONDecodeError:
            # Element may straddle the buffer edge: refill and retry; re-raise at EOF.
            chunk = reader.read(chunk_size)
            if not chunk:
                raise
            buf = buf[pos:] + chunk
            pos = 0
            continue
        yield element
        if pos > chunk_size:
            buf = buf[pos:]
            pos = 0


def _iter_zip_records(zip_path: Path) -> Iterator[object]:
    """Stream JSON-array records from the single member of a dump zip."""
    with zipfile.ZipFile(zip_path) as dump_zip, dump_zip.open(one(dump_zip.namelist())) as raw:
        yield from iter_json_array(io.TextIOWrapper(raw, encoding="utf-8"))


def iter_contracts(zip_path: Path) -> Iterator[ContractRecord]:
    for record in _iter_zip_records(zip_path):
        yield ContractRecord.model_validate(record)


def iter_comments(zip_path: Path) -> Iterator[CommentRecord]:
    for record in _iter_zip_records(zip_path):
        yield CommentRecord.model_validate(record)


def iter_bets(zip_path: Path) -> Iterator[BetRecord]:
    for record in _iter_zip_records(zip_path):
        yield BetRecord.model_validate(record)


def prob_at(bets: Iterable[BetRecord], when: datetime) -> float | None:
    """`probAfter` of the last bet at/before `when`, or None if no bet that early.

    The reconstruction rule verified in loom/plans/market_harvest.md: sort bets
    by createdTime, take the last bet at/before the cutoff, read its probAfter.
    """
    cutoff_ms = when.timestamp() * 1000
    eligible = [bet for bet in bets if bet.created_time <= cutoff_ms]
    if not eligible:
        return None
    return max(eligible, key=lambda bet: bet.created_time).prob_after
