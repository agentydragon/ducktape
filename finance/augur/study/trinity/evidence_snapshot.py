"""Materialize an evidence directory from the public upstreams, without the mirror.

`finance/evidence/checkout.py` clones the augur-evidence repo, which needs a read credential.
A study runs on the same bytes from the same source specs, so it can fetch them directly and
skip the mirror — what the mirror adds is history, freshness policy and an atomic "latest",
none of which a one-shot reproduction needs.

**Deviation from `scraper/fetch.py:write_sources`**, which does the same GETs: that one
tolerates a per-source failure and returns the failed set, because a daily CronJob must commit
whatever the healthy upstreams returned rather than lose them all to one outage. A study must
not: a missing series silently shortens the record it replays, and every number downstream is
then about a period nobody chose. So this raises.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path

from finance.evidence.sources import EvidenceSource
from finance.scraper.http_fetch import HttpGet, http_get as _real_http_get

logger = logging.getLogger(__name__)


async def snapshot_evidence(
    directory: Path, evidence_sources: Iterable[EvidenceSource], *, http_get: HttpGet = _real_http_get
) -> None:
    """GET every source concurrently into `directory/<output_filename>`, as raw bytes.

    Bytes, not parsed content: a binary upstream (Ken French ships a zip) needs nothing
    special, and the loader that reads the directory back is the one that knows the format.
    """

    evidence_sources = list(evidence_sources)
    bodies = await asyncio.gather(*(http_get(s.upstream_url, s.user_agent) for s in evidence_sources))
    for source, body in zip(evidence_sources, bodies, strict=True):
        (directory / source.output_filename).write_bytes(body)
        logger.info("fetched %s -> %s (%d bytes)", source.provenance_label, source.output_filename, len(body))
