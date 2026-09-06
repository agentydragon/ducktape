"""Held-to-maturity bonds, nominal and inflation-indexed.

A coupon is cash and it is interest, and which jurisdictions tax it is a relation between
the issuer and the holder rather than a property of the bond. That relation is what the
per-source income ledger exists for: a California resident holding a Treasury owes federal
tax on the coupon and nothing to California (31 USC 3124), and holding a California muni
owes neither (IRC 103, plus own-issue).

TIPS sit here rather than in a suite of their own because they are the same instrument with
one flag, and the sharpest thing to say about them is a comparison: on a flat CPI an indexed
bond must behave *exactly* like a nominal one, and on a rising one it must not. The CPI path
is a step rather than a drift, so the accretion lands in one identifiable month and every
assertion is an exact number.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import polars as pl
import pytest

from finance.augur.model.series import InflationKey
from finance.augur.sim.backend import CompiledRun, Engine
from finance.augur.sim.scenario import BondHolding, Scenario
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

QUANTA_PER_UNIT = 100
HORIZON = 14
# Long enough to outlive the horizon, for the cases that are about coupons rather than
# redemption.
NEVER_MATURES = 120

FACE = Decimal(1_000_000)
NOMINAL_RATE = 0.04
# Semiannual on a $1M face at 4%, before any indexation.
NOMINAL_COUPON = Decimal(20_000)

# CPI doubles in one step at month 6 and is flat elsewhere, so the accretion is attributable
# to exactly one month. The deflating path ends below par, which is what a floor is for.
CPI_DOUBLING = [100.0] * 6 + [200.0] * (HORIZON + 1 - 6)
CPI_FLAT = [100.0] * (HORIZON + 1)
CPI_DEFLATING = [100.0] * 6 + [80.0] * (HORIZON + 1 - 6)

TREASURY, MUNI, CORPORATE = "federal_us", "california", None


def bond_case(
    *,
    issuer: str | None = TREASURY,
    indexed: bool = False,
    cpi: list[float] | None = None,
    is_taxed: bool = True,
    maturity: int = NEVER_MATURES,
) -> Case:
    """One agent holding one bond, and nothing else that moves money.

    `is_taxed=False` is the scenario's way of saying "intentionally untaxed", which is what
    the pure-cashflow cases want: with a tax profile the year-end settlement lands in the
    same month as a coupon and the two net against each other.
    """

    return Case(
        scenario=bond_scenario(issuer=issuer, indexed=indexed, is_taxed=is_taxed, maturity=maturity),
        rollout_count=1,
        series={InflationKey(): levels([[Decimal(str(level)) for level in cpi or CPI_FLAT]])},
    )


def bond_scenario(
    *,
    issuer: str | None = TREASURY,
    indexed: bool = False,
    is_taxed: bool = True,
    maturity: int = NEVER_MATURES,
    account_id: str = "checking",
) -> Scenario:
    """The scenario alone, for the cases that are about authoring one rather than running it."""

    return scenario(
        checking(("alice", Decimal(100_000)), ("irs", Decimal(0))),
        initial_bonds=[
            BondHolding(
                bond_id="rung",
                agent_id="alice",
                account_id=account_id,
                issuer_jurisdiction_id=issuer,
                face_value=FACE,
                purchase_price=FACE,
                annual_coupon_rate=NOMINAL_RATE,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=maturity,
                inflation_indexed=indexed,
            )
        ],
        tax_profiles=[taxed("alice", "federal_us", "california")] if is_taxed else [],
        horizon_months=HORIZON,
    )


def _quanta(amount: Decimal | int | float) -> int:
    return int(Decimal(str(amount)) * QUANTA_PER_UNIT)


def _cash_by_month(result: SimulationResult) -> dict[int, int]:
    """What each month did to alice's cash.

    Snapshot 0 is the opening balance and snapshot `m + 1` is the balance once month `m` has
    run, so a cashflow in month `m` is the delta into `m + 1`. Keying by month rather than by
    snapshot keeps that offset in one place.
    """

    balances = (
        result.cash.filter(pl.col("agent_id") == "alice").sort("month_index").get_column("balance_quanta").to_list()
    )
    return {month: after - before for month, (before, after) in enumerate(pairwise(balances))}


def _paid(result: SimulationResult) -> dict[int, int]:
    return {month: delta for month, delta in _cash_by_month(result).items() if delta}


def _tax_by_jurisdiction(result: SimulationResult) -> dict[str, int]:
    accruals = result.events.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_quanta").sum())
    return dict(accruals.iter_rows())


def _alice_income(result: SimulationResult) -> pl.DataFrame:
    return result.income.filter((pl.col("agent_id") == "alice") & (pl.col("income_quanta") > 0))


class BondAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_coupons_arrive_as_cash_on_their_schedule(self, backend: Backend) -> None:
        """Semiannual, so months 6 and 12 and nothing in between — a bond bought today does
        not pay today, and it does not dribble monthly."""

        assert _paid(backend(bond_case(is_taxed=False))) == {6: _quanta(NOMINAL_COUPON), 12: _quanta(NOMINAL_COUPON)}

    def test_a_treasury_coupon_is_federally_taxed_and_california_exempt(self, backend: Backend) -> None:
        tax = _tax_by_jurisdiction(backend(bond_case(issuer=TREASURY)))

        assert tax["federal_us"] > 0
        assert tax["california"] == 0

    def test_an_in_state_muni_coupon_is_exempt_everywhere(self, backend: Backend) -> None:
        tax = _tax_by_jurisdiction(backend(bond_case(issuer=MUNI)))

        assert tax["federal_us"] == 0
        assert tax["california"] == 0

    def test_a_corporate_coupon_is_taxed_by_both(self, backend: Backend) -> None:
        """A `None` issuer is a real state, not a missing one: a non-governmental issuer that
        no jurisdiction exempts."""

        tax = _tax_by_jurisdiction(backend(bond_case(issuer=CORPORATE)))

        assert tax["federal_us"] > 0
        assert tax["california"] > 0

    def test_a_coupon_accrues_as_interest_and_not_as_ordinary_income(self, backend: Backend) -> None:
        """Which row it lands in is what decides whether California can reach it."""

        december = _alice_income(backend(bond_case(issuer=TREASURY))).filter(pl.col("month_index") == 11)

        assert december.get_column("income_source").to_list() == ["interest:federal_us"]
        assert december.get_column("income_quanta").to_list() == [_quanta(NOMINAL_COUPON)]

    def test_redemption_returns_the_face_as_cash_without_being_income(self, backend: Backend) -> None:
        """Getting the principal back is a return of capital, not a coupon. At par against a
        par basis it is not a capital gain either, so it moves cash and touches no income row.
        """

        maturity = 12
        # The maturity month pays its final coupon AND returns the face.
        assert _cash_by_month(backend(bond_case(is_taxed=False, maturity=maturity)))[maturity] == _quanta(
            FACE + NOMINAL_COUPON
        )

        # Across the whole run rather than at one month, so it does not depend on which
        # snapshot a tax year turns over on: a $1M face reaching income would tower over the
        # coupons in SOME row, whichever row that is.
        income = _alice_income(backend(bond_case(maturity=maturity))).get_column("income_quanta").to_list()
        assert max(income) == _quanta(NOMINAL_COUPON)

    def test_a_bond_paying_into_a_nonexistent_account_is_rejected(self, backend: Backend) -> None:
        """An unresolvable account on a POSITION is a typo, not a counterparty.

        Unmodeled counterparties legitimately settle against the external account, so without
        this guard a mistyped account would quietly hand alice's own coupons to the rest of
        the world — and the books would still balance, which is exactly why the conservation
        invariant cannot catch it and an explicit rejection has to.
        """

        mistyped = Case(
            scenario=bond_scenario(account_id="brokerage"),
            rollout_count=1,
            series={InflationKey(): levels([[Decimal(str(level)) for level in CPI_FLAT]])},
        )
        with pytest.raises(ValueError, match="has no cash account in this scenario"):
            backend(mistyped)

    def test_an_indexed_coupon_rides_the_indexed_principal(self, backend: Backend) -> None:
        """CPI doubles at month 6, so the month-6 coupon is still nominal — indexation applies
        at the payment and CPI has only just stepped — and the month-12 coupon is doubled."""

        assert _paid(backend(bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False))) == {
            6: _quanta(2 * NOMINAL_COUPON),
            12: _quanta(2 * NOMINAL_COUPON),
        }

    def test_a_nominal_bond_ignores_the_same_cpi_path(self, backend: Backend) -> None:
        """The control: same terms, same CPI, not indexed. Without it the divergence above
        could be something else the inflation path changed."""

        assert _paid(backend(bond_case(indexed=False, cpi=CPI_DOUBLING, is_taxed=False))) == {
            6: _quanta(NOMINAL_COUPON),
            12: _quanta(NOMINAL_COUPON),
        }

    def test_accretion_is_income_with_no_cash_behind_it(self, backend: Backend) -> None:
        """Phantom income, and the reason a TIPS loses to a muni after tax in some scenarios.

        CPI doubles at month 6, so principal rises $1M that month. That $1M is taxable
        interest the moment it accrues, and no cash moves for it.
        """

        indexed = backend(bond_case(indexed=True, cpi=CPI_DOUBLING))
        income = _alice_income(indexed).get_column("income_quanta").to_list()

        # Year one holds ONE coupon — the month-12 one is next tax year — doubled by the CPI
        # step, plus the full $1M of accretion. Accretion dwarfing the coupon is the point.
        assert max(income) == _quanta(2 * NOMINAL_COUPON + FACE)
        # And month 6 moved only the coupon in cash.
        untaxed = backend(bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False))
        assert _cash_by_month(untaxed)[6] == _quanta(2 * NOMINAL_COUPON)

    def test_accretion_is_treasury_interest_and_inherits_its_exemption(self, backend: Backend) -> None:
        """Accretion is interest on the same obligation, so 31 USC 3124 reaches it like a
        coupon. Booked as ordinary income instead, California would tax it."""

        tax = _tax_by_jurisdiction(backend(bond_case(indexed=True, cpi=CPI_DOUBLING)))

        assert tax["federal_us"] > 0
        assert tax["california"] == 0

    def test_redemption_is_floored_at_par_when_prices_fall(self, backend: Backend) -> None:
        """The deflation floor. CPI ends at 80% of its purchase level, so indexed principal is
        $800k — and a TIPS redeems at par, which is the promise a floor exists to make."""

        deflated = backend(bond_case(indexed=True, cpi=CPI_DEFLATING, is_taxed=False, maturity=12))

        # The final coupon rides the deflated principal; the principal itself comes back whole.
        assert _cash_by_month(deflated)[12] == _quanta(FACE + Decimal("0.8") * NOMINAL_COUPON)

    def test_a_flat_cpi_makes_an_indexed_bond_behave_exactly_like_a_nominal_one(self, backend: Backend) -> None:
        """Indexation with no inflation must be the identity, not merely close. Both paths are
        integer cents, so any rounding drift in the indexed branch shows here."""

        indexed = backend(bond_case(indexed=True, cpi=CPI_FLAT, is_taxed=False, maturity=12))
        nominal = backend(bond_case(indexed=False, cpi=CPI_FLAT, is_taxed=False, maturity=12))

        assert _cash_by_month(indexed) == _cash_by_month(nominal)


class BondValueAcceptance:
    """What the product read model carries a bond at. Inherit and supply `engine`."""

    @pytest.fixture
    def engine(self) -> Engine:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def _terminal_bond_value(self, engine: Engine, run: CompiledRun) -> int:
        return int(engine.product_metrics(run, primary_agent_id="alice").metric_arrays()["bond_value_quanta"][-1, 0])

    def test_net_worth_carries_an_indexed_bond_at_indexed_principal(self, engine: Engine) -> None:
        """Carrying it at par would understate net worth by the whole accretion — in exactly
        the inflationary scenarios a ladder is held for, which is the worst place to be wrong.

        Asserted against the nominal control on the same CPI path, which stays at $1M.
        Untaxed, because the tax on $1M of accretion is more than this holding's cash: the
        rollout would fail and report zeros, which is a different claim than this one.
        """

        indexed = bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False).compiled_run
        nominal = bond_case(indexed=False, cpi=CPI_DOUBLING, is_taxed=False).compiled_run

        assert self._terminal_bond_value(engine, indexed) == _quanta(2 * FACE)
        assert self._terminal_bond_value(engine, nominal) == _quanta(FACE)
