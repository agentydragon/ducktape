"""Shared schema for scraper-written evidence freshness metadata."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

EVIDENCE_META_FILENAME = "evidence_meta.json"


class EvidenceManifest(BaseModel):
    """Per-source last-successful-fetch times, committed as ``evidence_meta.json``.

    These timestamps attest when the scraper last fetched a source successfully. They are not
    source observation timestamps and do not imply when an upstream value was observed.
    """

    # provenance_label (e.g. "fred:CPIAUCSL") -> last successful fetch.
    last_fetched: dict[str, datetime] = Field(default_factory=dict)
