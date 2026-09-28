"""A parcel's ad-valorem law: its situs's jurisdiction tree resolved and quantized."""

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from more_itertools import one, only

from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.jurisdictions import BillRounding, Jurisdiction
from finance.augur.sim.money import Currency, round_ratio


@dataclass(frozen=True, kw_only=True)
class SitusLaw:
    """Proposition 13 from the state and the debt rates of the parcel's tax rate area.

    `inflation_factors` and `debt_rates` are keyed as `Proposition13.inflation_factors` and
    `Jurisdiction.debt_rates` are.
    """

    situs_id: JurisdictionId
    base_rate: Fraction
    inflation_cap: Fraction
    inflation_factors: Mapping[int, Fraction]
    homeowners_exemption: int
    supplemental_proration: Mapping[int, Fraction]
    debt_rates: Mapping[int, Fraction]
    bill_rounding: BillRounding | None

    def debt_rate(self, fiscal_year: int) -> Fraction:
        """The rate area's debt rate for the fiscal year starting in July of `fiscal_year`.

        Deviation: a fiscal year after the last published one carries that year's rate forward;
        the law sets each year's rate from that year's debt service, which is not known ahead.
        """
        # TODO: an unpublished fiscal year's debt rate should come from an exogenous series supplied
        # per rate area, the way CPI is, rather than the last published rate.
        if fiscal_year < min(self.debt_rates):
            raise ValueError(f"{self.situs_id!r} publishes no debt rate as early as fiscal year {fiscal_year}")
        return self.debt_rates[min(fiscal_year, max(self.debt_rates))]

    def secured_bill(self, taxable: int, fiscal_year: int) -> int:
        """A fiscal year's secured tax on `taxable` quanta, rounded as the rate area's collector rounds it."""
        rate = self.base_rate + self.debt_rate(fiscal_year)
        if self.bill_rounding is BillRounding.DOWN_TO_EVEN:
            return 2 * (taxable * rate.numerator // (2 * rate.denominator))
        return round_ratio(taxable * rate.numerator, rate.denominator)


def compile_situs(situs: Jurisdiction, *, currency: Currency) -> SitusLaw:
    """The law a parcel in `situs` is taxed under, taken from each level of its tree that sets it."""
    lineage = situs.lineage()
    proposition_13 = one(
        (level.proposition_13 for level in lineage if level.proposition_13 is not None),
        too_short=ValueError(f"no level above {situs.jurisdiction_id!r} sets Proposition 13"),
        too_long=ValueError(f"several levels above {situs.jurisdiction_id!r} set Proposition 13"),
    )
    debt_rates = one(
        (level.debt_rates for level in lineage if level.debt_rates is not None),
        too_short=ValueError(f"{situs.jurisdiction_id!r} is in no tax rate area"),
        too_long=ValueError(f"{situs.jurisdiction_id!r} is in several tax rate areas"),
    )
    if not debt_rates:
        raise ValueError(f"{situs.jurisdiction_id!r} publishes no debt rate")
    return SitusLaw(
        situs_id=situs.jurisdiction_id,
        base_rate=Fraction(proposition_13.base_rate),
        inflation_cap=Fraction(proposition_13.inflation_cap),
        inflation_factors={year: Fraction(factor) for year, factor in proposition_13.inflation_factors.items()},
        homeowners_exemption=currency.quanta(proposition_13.homeowners_exemption),
        supplemental_proration={
            month: Fraction(share) for month, share in proposition_13.supplemental_proration.items()
        },
        debt_rates={year: Fraction(rate) for year, rate in debt_rates.items()},
        bill_rounding=only(
            (level.bill_rounding for level in lineage if level.bill_rounding is not None),
            too_long=ValueError(f"several levels above {situs.jurisdiction_id!r} round secured bills"),
        ),
    )
