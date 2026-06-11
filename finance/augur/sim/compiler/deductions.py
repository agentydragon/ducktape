"""MID (Mortgage Interest Deduction, §163(h)(3)) and federal SALT (state and local tax
deduction) compile outputs. Both produce per-tax-link tables that the engine consumes
at year-end as part of the federal-itemize-vs-standard-deduction comparison."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.compiler.helpers import StringTable
from finance.augur.sim.compiler.properties import LiabilityCompileOutput
from finance.augur.sim.compiler.tax import TaxCompileOutput
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.scenario import MortgageInterestDeductionPolicy, Scenario


@dataclass(frozen=True)
class MIDCompileOutput:
    """Per-(tax_link, liability) Mortgage Interest Deduction (§163(h)(3)) plumbing.

    - `principal_ratio[link, lia]` = pro-rata `min(1, principal_cap[jurisdiction] /
      liability_principal[lia])` for liabilities owned by the link's profile agent and
      listed in a MortgageInterestDeductionPolicy; 0.0 otherwise. Engine does
      `interest_ytd @ principal_ratio[link]` per link to get MID per rollout.
    - `link_active[link]`: True iff that link has at least one non-zero principal_ratio
      entry; lets the engine skip the matmul + max for jurisdictions or scenarios
      without MID-eligible debt."""

    principal_ratio: NDArray[np.float64]
    link_active: NDArray[np.bool_]


@dataclass(frozen=True)
class SaltCompileOutput:
    """Per-tax-link federal SALT-deduction plumbing.

    - `link_active[link]`: True iff `link` is the federal jurisdiction of a profile with a
      FederalSaltDeductionPolicy. Federal SALT deduction is only computed for these links.
    - `cap_by_year[link, year]`: per-calendar-year SALT cap in USD for SALT-active links;
      0.0 elsewhere (and unread on the engine side). Year index 0 = first horizon year.
    - `contributing_mask[link, other_link]`: True iff `other_link` is a non-federal sibling
      (same profile) of the SALT-active federal `link`. Engine sums the first-pass annual
      tax of these state links into the federal SALT total."""

    link_active: NDArray[np.bool_]
    cap_by_year: NDArray[np.int64]
    contributing_mask: NDArray[np.bool_]


def compile_mortgage_interest_deductions(
    scenario: Scenario, strings: StringTable, *, tax: TaxCompileOutput, liabilities: LiabilityCompileOutput
) -> MIDCompileOutput:
    """Compile the precomputed per-(tax_link, liability) MID ratio matrix.

    For each (link, liability) pair, the ratio is the pro-rata
    `min(1, principal_cap / origination_principal)` factor applied to YTD interest
    when the engine sums MID at year-end. Zero where: (a) the liability isn't
    owned by the link's profile agent, (b) the liability has no
    MortgageInterestDeductionPolicy entry, (c) the policy's
    per_jurisdiction_principal_cap_usd map omits the link's jurisdiction, or
    (d) the policy's `debt_class == "home_equity"` (TCJA disallow §163(h)(3)).
    """

    link_count = tax.link_profile.shape[0]
    liability_count = liabilities.codes.shape[0]
    ratio = np.zeros((max(1, link_count), max(1, liability_count)), dtype=np.float64)
    active = np.zeros(max(1, link_count), dtype=np.bool_)

    if link_count == 0 or liability_count == 0 or not scenario.mortgage_interest_deduction_policies:
        return MIDCompileOutput(principal_ratio=ratio, link_active=active)

    liability_slot_by_code = {int(liabilities.codes[lia]): lia for lia in range(liability_count)}
    policies_by_liability_slot: dict[int, MortgageInterestDeductionPolicy] = {}
    for policy in scenario.mortgage_interest_deduction_policies:
        liability_code = strings.require(policy.liability_id)
        if liability_code not in liability_slot_by_code:
            raise ValueError(
                f"mortgage_interest_deduction_policies references unknown liability_id "
                f"{policy.liability_id!r}; known liabilities: {sorted(strings.values[int(c)] for c in liabilities.codes)}"
            )
        lia_slot = liability_slot_by_code[liability_code]
        owner_code = strings.require(policy.owner_agent_id)
        if int(liabilities.agent[lia_slot]) != owner_code:
            raise ValueError(
                f"mortgage_interest_deduction_policies owner_agent_id={policy.owner_agent_id!r} does not match "
                f"the liability's owner for liability_id={policy.liability_id!r}"
            )
        policies_by_liability_slot[lia_slot] = policy

    for link in range(link_count):
        profile_index = int(tax.link_profile[link])
        link_agent_code = int(tax.profile_agent[profile_index])
        jurisdiction_id = strings.values[int(tax.link_jurisdiction[link])]
        for lia_slot, policy in policies_by_liability_slot.items():
            if int(liabilities.agent[lia_slot]) != link_agent_code:
                continue
            if policy.debt_class == "home_equity":
                # TCJA (§163(h)(3), 2018-2025): home-equity-debt interest is not deductible.
                # Leave ratio[link, lia_slot] at 0.0 so the engine sums in nothing for this
                # liability. Callers who layer a HELOC-for-improvement should tag it
                # "acquisition" — we do not model the substantial-improvement carve-out.
                continue
            cap = policy.per_jurisdiction_principal_cap_usd.get(jurisdiction_id)
            if cap is None:
                continue
            principal = float(liabilities.principal[lia_slot])
            if principal <= 0.0:
                continue
            # Principal-cap ratio only. The owner-vs-rented split is now applied at runtime
            # via parallel `liability_rental_interest_ytd` accumulation that mirrors
            # `current.property_rented_fraction` — mid-horizon lifecycle events take effect
            # immediately in MID/Schedule E.
            ratio[link, lia_slot] = min(1.0, float(usd_to_cents(cap)) / principal)
        active[link] = bool(np.any(ratio[link] > 0.0))

    return MIDCompileOutput(principal_ratio=ratio, link_active=active)


def compile_federal_salt_deductions(
    scenario: Scenario, strings: StringTable, *, tax: TaxCompileOutput
) -> SaltCompileOutput:
    """Compile federal SALT-deduction plumbing.

    Returns three arrays sized to the tax-link grid:

    - `salt_active[link]`: True iff `link` is the federal jurisdiction of a profile
      with a FederalSaltDeductionPolicy.
    - `salt_cap_by_year[link, year]`: per-calendar-year SALT cap in USD for SALT-active
      links; the schedule's cap entries are forward-filled across the horizon.
    - `contributing_mask[link, other_link]`: True iff `other_link` is a non-federal
      sibling (same profile) of the SALT-active federal `link`. Engine sums the
      first-pass annual tax of these state links into the federal SALT total.
    """

    link_count = tax.link_profile.shape[0]
    horizon = int(scenario.horizon_months)
    year_count = max(1, (horizon + 11) // 12)
    salt_active = np.zeros(max(1, link_count), dtype=np.bool_)
    salt_cap_by_year = np.zeros((max(1, link_count), year_count), dtype=np.int64)
    contributing_mask = np.zeros((max(1, link_count), max(1, link_count)), dtype=np.bool_)

    if link_count == 0 or not scenario.federal_salt_deduction_policies:
        return SaltCompileOutput(
            link_active=salt_active, cap_by_year=salt_cap_by_year, contributing_mask=contributing_mask
        )

    # Map (profile_index, jurisdiction_code) -> link_index for cross-link lookups.
    link_by_profile_jurisdiction: dict[tuple[int, int], int] = {}
    for link in range(link_count):
        profile_idx = int(tax.link_profile[link])
        jur_code = int(tax.link_jurisdiction[link])
        link_by_profile_jurisdiction[(profile_idx, jur_code)] = link

    profile_index_by_agent: dict[int, int] = {
        strings.require(p.agent_id): i for i, p in enumerate(scenario.tax_profiles)
    }

    for policy in scenario.federal_salt_deduction_policies:
        profile_agent_code = strings.require(policy.profile_id)
        profile_index = profile_index_by_agent.get(profile_agent_code)
        if profile_index is None:
            raise ValueError(
                f"federal_salt_deduction_policies profile_id={policy.profile_id!r} does not match "
                f"any TaxProfile.agent_id"
            )
        federal_jur_code = strings.require(policy.federal_jurisdiction_id)
        federal_link = link_by_profile_jurisdiction.get((profile_index, federal_jur_code))
        if federal_link is None:
            raise ValueError(
                f"federal_salt_deduction_policies profile_id={policy.profile_id!r} does not have a "
                f"tax link for federal_jurisdiction_id={policy.federal_jurisdiction_id!r}"
            )
        salt_active[federal_link] = True
        for sibling in range(link_count):
            if sibling == federal_link:
                continue
            if int(tax.link_profile[sibling]) != profile_index:
                continue
            contributing_mask[federal_link, sibling] = True

        # Forward-fill the cap schedule across the horizon's calendar years. Entries are
        # tuples (effective_year_index, cap_usd); for each year, pick the latest entry whose
        # effective_year_index <= year. If no entry applies (e.g. schedule starts at year 2),
        # the cap is 0 (no allowed deduction). An empty schedule means SALT is effectively
        # uncapped — represent that by a large sentinel cap.
        if not policy.cap_schedule:
            salt_cap_by_year[federal_link, :] = np.iinfo(np.int64).max
            continue
        sorted_entries = sorted(policy.cap_schedule, key=lambda entry: entry.effective_year_index)
        for year in range(year_count):
            applicable = [entry for entry in sorted_entries if entry.effective_year_index <= year]
            if not applicable:
                salt_cap_by_year[federal_link, year] = 0.0
            else:
                salt_cap_by_year[federal_link, year] = usd_to_cents(applicable[-1].cap_usd)

    return SaltCompileOutput(link_active=salt_active, cap_by_year=salt_cap_by_year, contributing_mask=contributing_mask)
