"""A single filer's tax authority over the shipped jurisdiction laws."""

from collections.abc import Sequence

from finance.augur.sim.ids import AgentId, JurisdictionId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.money import USD, Currency
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile


def filer(
    agent_id: AgentId, jurisdiction_ids: Sequence[JurisdictionId], *, authority: AgentId, currency: Currency = USD
) -> TaxAuthority:
    """The authority taxing `agent_id` as a single filer under the shipped laws of `jurisdiction_ids`, in nominal
    dollars, with payments going from the filer's `checking` to `authority`'s."""
    profile = TaxProfile(agent_id=agent_id, jurisdiction_ids=list(jurisdiction_ids), tax_authority_agent_id=authority)
    laws = {id_: load_jurisdiction(id_) for id_ in jurisdiction_ids}
    return TaxAuthority(compile_profile(profile, laws, currency=currency), indexation=FixedNominalLaw())
