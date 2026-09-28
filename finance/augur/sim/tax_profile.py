"""A taxpayer's filing status, jurisdictions and tax-payment routing, and their resolution into
exact, variable-length tax records."""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from finance.augur.sim import tax
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.ids import CHECKING, AccountId, AgentId, JurisdictionId
from finance.augur.sim.income import OrdinaryIncome, TransferIncomeCategory, income_source_sort_key
from finance.augur.sim.jurisdictions import (
    IncomeTax,
    Jurisdiction,
    StatutoryAmount,
    StatutoryIndexation,
    TaxBracket,
    ThresholdTax,
)
from finance.augur.sim.money import Currency, NonNegativeCurrencyAmount


class FilingStatus(StrEnum):
    """Federal/state filing status. Today only single-filer is wired through the tax + §121
    math; adding a new variant requires touching every place that branches on filing status
    (bracket lookup keys in jurisdiction YAMLs, §121 cap table in `_apply_property_sale`,
    standard-deduction lookup, …). The enum makes this an explicit blocker on every
    callsite rather than a string typo silently falling through to a missing-key error."""

    SINGLE = "single"


class TaxProfile(BaseModel):
    """A taxed agent's tax-time configuration. At spike 1 only single filers are modeled;
    later layers add MFJ / HoH and any filing-status-driven branching."""

    agent_id: AgentId
    filing_status: FilingStatus = FilingStatus.SINGLE
    jurisdiction_ids: list[JurisdictionId] = Field(
        description='Ordered list of taxing authorities — typically `["federal_us", "california"]` for a CA resident.'
    )
    tax_authority_agent_id: AgentId = Field(
        description="Destination of tax-payment transfers — a bookkeeping sink, not a taxed agent itself."
    )
    payment_account_id: AccountId = Field(
        default=CHECKING, description="The agent's account the engine debits for estimated-tax and true-up payments."
    )
    tax_authority_account_id: AccountId = Field(
        default=CHECKING, description="The matching credit account on the tax authority's side."
    )
    prior_year_tax: NonNegativeCurrencyAmount = Field(
        default=Decimal(0),
        description=(
            "Aggregate safe-harbor target used to size quarterly estimated payments. If "
            "0, no quarterly estimates are emitted and the January true-up pays the full "
            "accrued tax."
        ),
    )


SECTION_1250_FEDERAL_CAP_RATE = Decimal("0.25")
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


def compile_income_sources(named: Iterable[TransferIncomeCategory]) -> tuple[TransferIncomeCategory, ...]:
    """Ordinary income plus every category the cashflows, held bonds or fund distributions name, in reporting order."""

    return tuple(sorted({OrdinaryIncome(), *named}, key=income_source_sort_key))


def _agreed_capital_loss_offset_cap(
    profile: TaxProfile, laws: Mapping[JurisdictionId, IncomeTax], *, currency: Currency
) -> int:
    """Netting runs once per taxpayer; reject jurisdictions requiring different offset caps, now or once indexed."""

    caps = {
        jurisdiction_id: (
            law.max_capital_loss_ordinary_offset[profile.filing_status],
            law.indexation[StatutoryAmount.MAX_CAPITAL_LOSS_ORDINARY_OFFSET],
        )
        for jurisdiction_id, law in laws.items()
    }
    if len(set(caps.values())) > 1:
        raise ValueError(
            f"tax profile for {profile.agent_id!r} spans jurisdictions that cap the capital-loss "
            f"ordinary offset differently ({caps}); one netting per taxpayer cannot answer for both"
        )
    return currency.quanta(next(iter(caps.values()))[0])


def _brackets(brackets: Sequence[TaxBracket], *, currency: Currency) -> tuple[tax.TaxBracket, ...]:
    return tuple(
        tax.TaxBracket(
            upper=None if bracket.upper == "Infinity" else currency.quanta(bracket.upper),
            rate_ppb=rate_to_ppb(bracket.rate),
        )
        for bracket in brackets
    )


def _threshold_tax(
    statutory: ThresholdTax | None, filing_status: FilingStatus, *, currency: Currency
) -> tax.ThresholdTax | None:
    if statutory is None:
        return None
    return tax.ThresholdTax(
        rate_ppb=rate_to_ppb(statutory.rate), threshold=currency.quanta(statutory.threshold[filing_status])
    )


def compile_profile(
    profile: TaxProfile, jurisdictions: Mapping[JurisdictionId, Jurisdiction], *, currency: Currency
) -> tax.TaxProfile:
    """One taxpayer's routing and quantized rules, as a composed world enrolls them."""
    laws = {}
    for jurisdiction_id in profile.jurisdiction_ids:
        law = jurisdictions[jurisdiction_id].income_tax
        if law is None:
            raise ValueError(
                f"tax profile for {profile.agent_id!r} names {jurisdiction_id!r}, which levies no income tax"
            )
        laws[jurisdiction_id] = law
    offset_cap = _agreed_capital_loss_offset_cap(profile, laws, currency=currency)
    rules = []
    for jurisdiction_id, law in laws.items():
        rules.append(
            tax.TaxRules(
                jurisdiction_id=jurisdiction_id,
                exempt_interest=law.exempt_interest,
                ordinary_brackets=_brackets(law.ordinary_income_brackets[profile.filing_status], currency=currency),
                long_term_capital_gain_brackets=(
                    _brackets(law.ltcg_brackets[profile.filing_status], currency=currency)
                    if law.ltcg_brackets is not None
                    else ()
                ),
                standard_deduction=currency.quanta(law.standard_deduction[profile.filing_status]),
                max_capital_loss_ordinary_offset=offset_cap,
                section_1250_rate_ppb=rate_to_ppb(
                    SECTION_1250_FEDERAL_CAP_RATE if jurisdiction_id == SECTION_1250_FEDERAL_JURISDICTION_ID else 0
                ),
                net_investment_income_tax=_threshold_tax(
                    law.net_investment_income_tax, profile.filing_status, currency=currency
                ),
                taxable_income_surtax=_threshold_tax(
                    law.taxable_income_surtax, profile.filing_status, currency=currency
                ),
                law_year=law.law_year,
                indexed=frozenset(
                    amount for amount, indexation in law.indexation.items() if indexation is StatutoryIndexation.CPI
                ),
            )
        )
    return tax.TaxProfile(
        agent_id=profile.agent_id,
        tax_authority_agent_id=profile.tax_authority_agent_id,
        payment_account_id=profile.payment_account_id,
        tax_authority_account_id=profile.tax_authority_account_id,
        prior_year_tax=currency.quanta(profile.prior_year_tax),
        section_121_exclusion=currency.quanta(section_121_exclusion_for(profile.filing_status)),
        jurisdictions=tuple(rules),
    )
