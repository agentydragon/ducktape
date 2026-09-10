"""Shared runtime semantics for simulator engines."""

from __future__ import annotations

from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.scenario import Scenario


def load_jurisdictions_for(scenario: Scenario) -> dict[str, Jurisdiction]:
    """Load every jurisdiction referenced by any scenario tax profile."""

    ids = {jurisdiction_id for profile in scenario.tax_profiles for jurisdiction_id in profile.jurisdiction_ids}
    return {jurisdiction_id: load_jurisdiction(jurisdiction_id) for jurisdiction_id in ids}
