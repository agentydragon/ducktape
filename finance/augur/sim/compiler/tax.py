"""Resolve filing-status schedules and income categories into exact, variable-length tax records."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from finance.augur.sim.compiler.bonds import bond_income_categories
from finance.augur.sim.compiler.distributions import distribution_income_categories
from finance.augur.sim.compiler.income_sources import income_source_sort_key
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket, load_jurisdiction
from finance.augur.sim.scenario import (
    FilingStatus,
    InterestIncome,
    OrdinaryIncome,
    Scenario,
    TaxProfile,
    TransferIncomeCategory,
)

SECTION_1250_FEDERAL_CAP_RATE = 0.25
SECTION_1250_FEDERAL_JURISDICTION_ID = "federal_us"

_SECTION_121_EXCLUSION_BY_FILING_STATUS: dict[FilingStatus, Decimal] = {FilingStatus.SINGLE: Decimal(250_000)}


def section_121_exclusion_for(filing_status: FilingStatus) -> Decimal:
    if filing_status not in _SECTION_121_EXCLUSION_BY_FILING_STATUS:
        raise NotImplementedError(
            f"§121 exclusion cap is not implemented for filing_status={filing_status!r}; "
            f"add a {filing_status} entry to _SECTION_121_EXCLUSION_BY_FILING_STATUS "
            f"and audit every other place that branches on filing status (jurisdiction "
            f"bracket lookups, standard-deduction lookups, MID, SALT cap, NIIT thresholds)."
        )
    return _SECTION_121_EXCLUSION_BY_FILING_STATUS[filing_status]


@dataclass(frozen=True)
class PreparedTaxBracket:
    """One marginal slice: inclusive upper edge in currency quanta, or no upper bound."""

    upper: int | None
    rate_ppb: int


@dataclass(frozen=True)
class PreparedTaxRules:
    """One jurisdiction's rules resolved for a taxpayer's filing status; money is integer quanta."""

    jurisdiction_id: str
    exempt_interest_from_levels: tuple[JurisdictionLevel, ...]
    exempts_own_issue: bool
    ordinary_brackets: tuple[PreparedTaxBracket, ...]
    long_term_capital_gain_brackets: tuple[PreparedTaxBracket, ...]
    standard_deduction: int
    max_capital_loss_ordinary_offset: int
    # Positive caps federal-style unrecaptured depreciation; zero uses ordinary brackets.
    section_1250_rate_ppb: int


@dataclass(frozen=True)
class PreparedTaxProfile:
    """A taxpayer's payment routing, quantized allowances and ordered jurisdiction rules."""

    agent_id: str
    tax_authority_agent_id: str
    payment_account_id: str
    tax_authority_account_id: str
    prior_year_tax: int
    section_121_exclusion: int
    jurisdictions: tuple[PreparedTaxRules, ...]


@dataclass(frozen=True)
class TaxCompileOutput:
    """Authoritative prepared profiles and income categories in their declared reporting order."""

    profiles: tuple[PreparedTaxProfile, ...]
    income_sources: tuple[TransferIncomeCategory, ...]


class IncomeTagged(Protocol):
    """The common income tag on transfers and property cashflows."""

    income_category: TransferIncomeCategory | None


def collect_income_sources(scenario: Scenario) -> set[TransferIncomeCategory]:
    """Every income category referenced by cashflows, held bonds or fund distributions."""

    tagged: tuple[IncomeTagged, ...] = (
        *scenario.scheduled_transfers,
        *scenario.recurring_transfers,
        *scenario.scheduled_property_cashflows,
        *scenario.recurring_property_cashflows,
    )
    return (
        {item.income_category for item in tagged if item.income_category is not None}
        | bond_income_categories(scenario)
        | distribution_income_categories(scenario)
    )


def _agreed_capital_loss_offset_cap(
    profile: TaxProfile, jurisdictions: Mapping[str, Jurisdiction], *, quantum: Decimal
) -> int:
    """Netting runs once per taxpayer; reject jurisdictions requiring different offset caps."""

    caps = {
        jurisdiction_id: jurisdictions[jurisdiction_id].max_capital_loss_ordinary_offset[profile.filing_status]
        for jurisdiction_id in profile.jurisdiction_ids
    }
    if len(set(caps.values())) > 1:
        raise ValueError(
            f"tax profile for {profile.agent_id!r} spans jurisdictions that cap the capital-loss "
            f"ordinary offset differently ({caps}); one netting per taxpayer cannot answer for both"
        )
    return int(currency_amount_to_quanta(next(iter(caps.values())), quantum=quantum))


def _brackets(brackets: Sequence[TaxBracket], *, quantum: Decimal) -> tuple[PreparedTaxBracket, ...]:
    return tuple(
        PreparedTaxBracket(
            upper=None
            if bracket.upper == "Infinity"
            else int(currency_amount_to_quanta(bracket.upper, quantum=quantum)),
            rate_ppb=rate_to_ppb(bracket.rate),
        )
        for bracket in brackets
    )


def compile_tax(scenario: Scenario, jurisdictions: Mapping[str, Jurisdiction]) -> TaxCompileOutput:
    quantum = scenario.currency.quantum
    profiles = []
    for profile in scenario.tax_profiles:
        offset_cap = _agreed_capital_loss_offset_cap(profile, jurisdictions, quantum=quantum)
        rules = []
        for jurisdiction_id in profile.jurisdiction_ids:
            jurisdiction = jurisdictions[jurisdiction_id]
            rules.append(
                PreparedTaxRules(
                    jurisdiction_id=jurisdiction_id,
                    exempt_interest_from_levels=tuple(sorted(jurisdiction.exempt_interest_from_levels)),
                    exempts_own_issue=jurisdiction.exempts_own_issue,
                    ordinary_brackets=_brackets(
                        jurisdiction.ordinary_income_brackets[profile.filing_status], quantum=quantum
                    ),
                    long_term_capital_gain_brackets=(
                        _brackets(jurisdiction.ltcg_brackets[profile.filing_status], quantum=quantum)
                        if jurisdiction.ltcg_brackets is not None
                        else ()
                    ),
                    standard_deduction=int(
                        currency_amount_to_quanta(
                            jurisdiction.standard_deduction[profile.filing_status], quantum=quantum
                        )
                    ),
                    max_capital_loss_ordinary_offset=offset_cap,
                    section_1250_rate_ppb=rate_to_ppb(
                        SECTION_1250_FEDERAL_CAP_RATE
                        if jurisdiction_id == SECTION_1250_FEDERAL_JURISDICTION_ID
                        else 0.0
                    ),
                )
            )
        profiles.append(
            PreparedTaxProfile(
                agent_id=profile.agent_id,
                tax_authority_agent_id=profile.tax_authority_agent_id,
                payment_account_id=profile.payment_account_id,
                tax_authority_account_id=profile.tax_authority_account_id,
                prior_year_tax=int(currency_amount_to_quanta(profile.prior_year_tax, quantum=quantum)),
                section_121_exclusion=int(
                    currency_amount_to_quanta(section_121_exclusion_for(profile.filing_status), quantum=quantum)
                ),
                jurisdictions=tuple(rules),
            )
        )

    sources = tuple(sorted({OrdinaryIncome(), *collect_income_sources(scenario)}, key=income_source_sort_key))
    # Every named issuer must resolve, including issuers found only on cashflows.
    for source in sources:
        if isinstance(source, InterestIncome) and source.issuer_jurisdiction_id is not None:
            load_jurisdiction(source.issuer_jurisdiction_id)
    return TaxCompileOutput(profiles=tuple(profiles), income_sources=sources)
