"""The default price-client wiring shared by every server entrypoint.

Every concrete deployment (`api.server`, `dev_server`, `calibration_report`) needs the
same `{Platform: PriceClient}` mapping, so the wiring lives here instead of being
duplicated: mirror-backed readers over the evidence checkout (in-cluster: the
git-sync'd `AUGUR_EVIDENCE_DIR`; workstations: `ensure_checkout()` clones with the
read credentials). Tests still construct their own hermetic clients directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from finance.augur.calibration.evidence_clients import EvidenceMarketReader
from finance.augur.calibration.platform import PriceClient
from finance.evidence.checkout import ensure_checkout
from finance.evidence.markets import Platform


@asynccontextmanager
async def default_price_clients() -> AsyncIterator[dict[Platform, PriceClient]]:
    """One process-lifetime set of mirror-backed readers; fails fast at startup when
    neither `AUGUR_EVIDENCE_DIR` nor the clone credentials are available."""
    evidence_dir = ensure_checkout()
    yield {platform: EvidenceMarketReader(platform=platform, evidence_dir=evidence_dir) for platform in Platform}
