"""Shared runtime semantics for simulator engines."""

from __future__ import annotations

from collections.abc import Iterable

from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.scenario import TaxProfile


def load_jurisdictions_for(profiles: Iterable[TaxProfile]) -> dict[JurisdictionId, Jurisdiction]:
    """Load every jurisdiction a tax profile names."""

    ids = {jurisdiction_id for profile in profiles for jurisdiction_id in profile.jurisdiction_ids}
    return {jurisdiction_id: load_jurisdiction(jurisdiction_id) for jurisdiction_id in ids}
