"""Resolve scenario tax profiles, jurisdiction rules, and income-source ordering
into the simulation engine's tax tables."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.sim.compiler.bonds import bond_income_categories
from finance.augur.sim.compiler.distributions import distribution_income_categories
from finance.augur.sim.compiler.helpers import StringTable
from finance.augur.sim.compiler.income_buckets import IncomeBuckets
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.jurisdictions import BracketUpper, Jurisdiction, load_jurisdiction
from finance.augur.sim.scenario import FilingStatus, InterestIncome, Scenario, TaxProfile, TransferIncomeCategory

SECTION_1250_FEDERAL_CAP_RATE = 0.25
SECTION_1250_FEDERAL_JURISDICTION_ID = "federal_us"

# §121 primary-residence exclusion cap (post-recapture LTCG that can be excluded from federal
# capital gains on a sale of a qualifying residence). Single filer: $250k. The lookup table
# is the single source of truth; new `FilingStatus` variants must add an entry here or
# `section_121_exclusion_for` raises NotImplementedError — which keeps "I forgot this
# branch" from silently falling through to a wrong tax number.
_SECTION_121_EXCLUSION_BY_FILING_STATUS: dict[FilingStatus, Decimal] = {FilingStatus.SINGLE: Decimal(250_000)}
OPEN_ENDED_BRACKET_UPPER_QUANTA = np.iinfo(np.int64).max


def section_121_exclusion_for(filing_status: FilingStatus) -> Decimal:
    if filing_status not in _SECTION_121_EXCLUSION_BY_FILING_STATUS:
        raise NotImplementedError(
            f"§121 exclusion cap is not implemented for filing_status={filing_status!r}; "
            f"add a {filing_status} entry to _SECTION_121_EXCLUSION_BY_FILING_STATUS "
            f"and audit every other place that branches on filing status (jurisdiction "
            f"bracket lookups, standard-deduction lookups, MID, SALT cap, NIIT thresholds)."
        )
    return _SECTION_121_EXCLUSION_BY_FILING_STATUS[filing_status]


def bracket_upper_to_quanta(upper: BracketUpper, *, currency_quantum: object) -> np.int64:
    if upper == "Infinity":
        return np.int64(OPEN_ENDED_BRACKET_UPPER_QUANTA)
    return currency_amount_to_quanta(upper, quantum=currency_quantum)


@dataclass(frozen=True)
class TaxCompileOutput:
    """Tax-profile + tax-link arrays produced by `compile_tax`. Each row of
    `profile_*` is one TaxProfile; each row of `link_*` is one (profile, jurisdiction)
    pair. `*_upper/*_rate/*_count` are rectangular bracket tables — `count[link]` is
    the active prefix length for that link's brackets (zero-padded beyond).

    Notable fields:

    - `profile_section_121_exclusion`: §121 primary-residence exclusion cap, USD.
      Looked up by filing status at compile time
      (`_SECTION_121_EXCLUSION_BY_FILING_STATUS`); only `single` is wired today
      ($250k). Engine reads on every property sale to compute the exclusion ceiling.
    - `profile_max_capital_loss_ordinary_offset`: IRC 1211(b) cap on how much of a net
      capital loss offsets ordinary income in one year. Per PROFILE rather than per link
      because the netting runs once per taxpayer, before any jurisdiction sees the result;
      `compile_tax` refuses a profile whose jurisdictions disagree, since that is exactly
      the case one netting cannot answer for both.
    - `link_section_1250_rate`: §1250 unrecaptured-depreciation rate cap. Positive ⇒
      federal-style flat rate (0.25 for `federal_us`); 0.0 ⇒ no separate cap, recapture
      is taxed as ordinary inside the standard bracket walk (state-style, e.g. CA)."""

    profile_prior_year_tax: Int64[np.ndarray, " tax_profile"]
    profile_section_121_exclusion: Int64[np.ndarray, " tax_profile"]
    profile_max_capital_loss_ordinary_offset: Int64[np.ndarray, " tax_profile"]
    link_profile: Int64[np.ndarray, " tax_link"]
    link_jurisdiction: Int64[np.ndarray, " tax_link"]
    link_standard_deduction: Int64[np.ndarray, " tax_link"]
    link_section_1250_rate: Float64[np.ndarray, " tax_link"]
    link_ordinary_upper: Int64[np.ndarray, " tax_link bracket"]
    link_ordinary_rate: Float64[np.ndarray, " tax_link bracket"]
    link_ordinary_count: Int64[np.ndarray, " tax_link"]
    link_ltcg_upper: Int64[np.ndarray, " tax_link bracket"]
    link_ltcg_rate: Float64[np.ndarray, " tax_link bracket"]
    link_ltcg_count: Int64[np.ndarray, " tax_link"]
    buckets: IncomeBuckets


class IncomeTagged(Protocol):
    """Anything that can hand an agent taxable income. Transfers and property cashflows are
    unrelated models that happen to share this one field, so the walk below needs the shape
    named rather than falling back to their common `BaseModel` base."""

    income_category: TransferIncomeCategory | None


def collect_income_sources(scenario: Scenario) -> set[TransferIncomeCategory]:
    """Every income source the scenario references, as the tags themselves.

    The tag IS the axis key — `OrdinaryIncome()` and `InterestIncome(issuer=...)` are
    already distinct, hashable values, so there is no sentinel-string vocabulary to invent
    and nothing for a jurisdiction id to collide with.
    """

    tagged: tuple[IncomeTagged, ...] = (
        *scenario.scheduled_transfers,
        *scenario.recurring_transfers,
        *scenario.scheduled_property_cashflows,
        *scenario.recurring_property_cashflows,
    )
    # Bonds and fund distributions are not `IncomeTagged` — each carries terms, and the
    # category is derived from the issuer rather than stored on the instrument. The axis still
    # has to carry a row for that issuer before their tables can name one.
    return (
        {item.income_category for item in tagged if item.income_category is not None}
        | bond_income_categories(scenario)
        | distribution_income_categories(scenario)
    )


def _agreed_capital_loss_offset_cap(
    profile: TaxProfile, jurisdictions: dict[str, Jurisdiction], *, quantum: object
) -> np.int64:
    """The one IRC 1211(b) cap this taxpayer nets against.

    The netting happens once per taxpayer and its result feeds every jurisdiction, so there is
    no answer to give when two of them cap the offset differently. Refusing here is the honest
    outcome: the alternative is silently picking one jurisdiction's rule and reporting numbers
    for the other that its own law does not support. Netting per jurisdiction is the change
    that would lift this, and it is not a change the reader can make on its own.
    """

    caps = {
        jurisdiction_id: jurisdictions[jurisdiction_id].max_capital_loss_ordinary_offset[profile.filing_status]
        for jurisdiction_id in profile.jurisdiction_ids
    }
    if len(set(caps.values())) > 1:
        raise ValueError(
            f"tax profile for {profile.agent_id!r} spans jurisdictions that cap the capital-loss "
            f"ordinary offset differently ({caps}); one netting per taxpayer cannot answer for both"
        )
    return currency_amount_to_quanta(next(iter(caps.values())), quantum=quantum)


def compile_tax(scenario: Scenario, strings: StringTable, jurisdictions: dict[str, Jurisdiction]) -> TaxCompileOutput:
    prior_year_tax: list[np.int64] = []
    link_profile: list[int] = []
    link_jurisdiction: list[int] = []
    standard_deduction: list[np.int64] = []
    section_1250_rate: list[float] = []
    ordinary_brackets: list[list[tuple[np.int64, float]]] = []
    ltcg_brackets: list[list[tuple[np.int64, float]]] = []
    section_121_exclusion: list[np.int64] = []
    max_capital_loss_ordinary_offset: list[np.int64] = []

    max_ord = 1
    max_ltcg = 1
    for profile_index, profile in enumerate(scenario.tax_profiles):
        prior_year_tax.append(currency_amount_to_quanta(profile.prior_year_tax, quantum=scenario.currency.quantum))
        section_121_exclusion.append(
            currency_amount_to_quanta(
                section_121_exclusion_for(profile.filing_status), quantum=scenario.currency.quantum
            )
        )
        max_capital_loss_ordinary_offset.append(
            _agreed_capital_loss_offset_cap(profile, jurisdictions, quantum=scenario.currency.quantum)
        )
        for jurisdiction_id in profile.jurisdiction_ids:
            jurisdiction = jurisdictions[jurisdiction_id]
            ordinary = [
                (
                    bracket_upper_to_quanta(bracket.upper, currency_quantum=scenario.currency.quantum),
                    float(bracket.rate),
                )
                for bracket in jurisdiction.ordinary_income_brackets[profile.filing_status]
            ]
            ltcg = (
                [
                    (
                        bracket_upper_to_quanta(bracket.upper, currency_quantum=scenario.currency.quantum),
                        float(bracket.rate),
                    )
                    for bracket in jurisdiction.ltcg_brackets[profile.filing_status]
                ]
                if jurisdiction.ltcg_brackets is not None
                else []
            )
            max_ord = max(max_ord, len(ordinary))
            max_ltcg = max(max_ltcg, len(ltcg))
            link_profile.append(profile_index)
            link_jurisdiction.append(strings.require(jurisdiction_id))
            standard_deduction.append(
                currency_amount_to_quanta(
                    jurisdiction.standard_deduction[profile.filing_status], quantum=scenario.currency.quantum
                )
            )
            # Federal-us gets the §1250 25% flat rate cap; all other jurisdictions tax
            # unrecaptured-depreciation as ordinary income (CA, etc.).
            section_1250_rate.append(
                SECTION_1250_FEDERAL_CAP_RATE if jurisdiction_id == SECTION_1250_FEDERAL_JURISDICTION_ID else 0.0
            )
            ordinary_brackets.append(ordinary)
            ltcg_brackets.append(ltcg)

    link_count = len(link_profile)

    # Preserve the declared income-source ordering used by execution inputs and outputs.
    buckets = IncomeBuckets.for_sources(collect_income_sources(scenario), profile_count=len(scenario.tax_profiles))
    # Every named income issuer must resolve, including issuers found only on cashflows.
    for source in buckets.source_ids:
        if isinstance(source, InterestIncome) and source.issuer_jurisdiction_id is not None:
            load_jurisdiction(source.issuer_jurisdiction_id)

    ordinary_upper = np.zeros((max(1, link_count), max_ord), dtype=np.int64)
    ordinary_rate = np.zeros((max(1, link_count), max_ord), dtype=np.float64)
    ordinary_count = np.zeros(max(1, link_count), dtype=np.int64)
    ltcg_upper = np.zeros((max(1, link_count), max_ltcg), dtype=np.int64)
    ltcg_rate = np.zeros((max(1, link_count), max_ltcg), dtype=np.float64)
    ltcg_count = np.zeros(max(1, link_count), dtype=np.int64)
    for idx, ordinary in enumerate(ordinary_brackets):
        ordinary_count[idx] = len(ordinary)
        for bracket_idx, (upper, rate) in enumerate(ordinary):
            ordinary_upper[idx, bracket_idx] = upper
            ordinary_rate[idx, bracket_idx] = rate
    for idx, ltcg in enumerate(ltcg_brackets):
        ltcg_count[idx] = len(ltcg)
        for bracket_idx, (upper, rate) in enumerate(ltcg):
            ltcg_upper[idx, bracket_idx] = upper
            ltcg_rate[idx, bracket_idx] = rate

    return TaxCompileOutput(
        profile_prior_year_tax=np.asarray(prior_year_tax, dtype=np.int64),
        profile_section_121_exclusion=np.asarray(section_121_exclusion, dtype=np.int64),
        profile_max_capital_loss_ordinary_offset=np.asarray(max_capital_loss_ordinary_offset, dtype=np.int64),
        link_profile=np.asarray(link_profile, dtype=np.int64),
        link_jurisdiction=np.asarray(link_jurisdiction, dtype=np.int64),
        link_standard_deduction=np.asarray(standard_deduction, dtype=np.int64),
        link_section_1250_rate=np.asarray(section_1250_rate, dtype=np.float64),
        buckets=buckets,
        link_ordinary_upper=ordinary_upper,
        link_ordinary_rate=ordinary_rate,
        link_ordinary_count=ordinary_count,
        link_ltcg_upper=ltcg_upper,
        link_ltcg_rate=ltcg_rate,
        link_ltcg_count=ltcg_count,
    )
