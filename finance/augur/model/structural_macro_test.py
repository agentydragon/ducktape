"""Tests for the structural macro provider.

The assertions here are about the STRUCTURE, because the structure is the whole reason this
provider exists. Nothing checks a hand-set parameter against itself; every test names an
economic relation the model claims — a rate rise moves price down and payout up, a longer
fund moves further, a fund's payout lags its market yield, zero duration is cash — and would
fail if the relation broke, including if it broke by the price and payout being sampled
independently.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    level_series_request_channels,
    validate_sample_satisfies_request,
)
from finance.augur.model.provider_config import ProviderConfig
from finance.augur.model.series import (
    InflationKey,
    LevelSeriesKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.model.structural_macro import (
    MINIMUM_ANNUAL_YIELD,
    EquitySpec,
    InstrumentSpec,
    StructuralMacroProviderConfig,
)

HORIZON = 60
SEEDS = (11, 22, 33)

BOND = SecuritySymbol("BOND")
CASH = SecuritySymbol("CASH")
EQUITY = SecuritySymbol("EQ")


def _config(**updates: object) -> StructuralMacroProviderConfig:
    """A deterministic two-instrument config: every shock scale is zero.

    With no shocks the latent rates path is the bare mean-reversion curve, so a test can state
    "the short rate rises over the horizon" by choosing `initial_short_rate` below
    `short_rate_mean` and then assert what that rise does — no sampling noise, no tolerance
    bands, no seed to get lucky with.
    """

    fields: dict[str, object] = {
        "short_rate_monthly_sigma": 0.0,
        "term_spread_monthly_sigma": 0.0,
        "inflation_monthly_log_sigma": 0.0,
        "instruments": (
            InstrumentSpec(symbol=BOND, duration_years=6.0, initial_price_usd=100.0),
            InstrumentSpec(symbol=CASH, duration_years=0.0, initial_price_usd=1.0),
        ),
        **updates,
    }
    return StructuralMacroProviderConfig.model_validate(fields)


def _sample(config: StructuralMacroProviderConfig, *, horizon_months: int = HORIZON) -> SampledExogenousBundle:
    return config.realize_model().sample(ExogenousSamplingRequest(horizon_months=horizon_months, rollout_seeds=SEEDS))


def _series(bundle: SampledExogenousBundle, key: LevelSeriesKey, *, horizon_months: int = HORIZON) -> np.ndarray:
    return bundle.level_matrix(key, rollout_count=len(SEEDS), horizon_months=horizon_months)


def _rising_rates() -> StructuralMacroProviderConfig:
    return _config(initial_short_rate=0.01, short_rate_mean=0.06)


def _falling_rates() -> StructuralMacroProviderConfig:
    return _config(initial_short_rate=0.06, short_rate_mean=0.01)


def test_rate_rise_moves_price_down_and_payout_up_together() -> None:
    """The relation the whole model exists to produce, and the one an independently-fitted
    price series and payout series cannot produce: 2022's shape, where a fund lost value and
    started paying more."""

    bundle = _sample(_rising_rates())
    price = _series(bundle, SecurityKey(symbol=BOND))
    payout = _series(bundle, SecurityDistributionKey(symbol=BOND))

    assert np.all(price[:, -1] < price[:, 0])
    assert np.all(payout[:, -1] > payout[:, 0])


def test_rate_fall_moves_price_up_and_payout_down_together() -> None:
    """The same relation with the sign flipped, so the test above cannot pass on a model that
    simply makes bond prices fall."""

    bundle = _sample(_falling_rates())
    price = _series(bundle, SecurityKey(symbol=BOND))
    payout = _series(bundle, SecurityDistributionKey(symbol=BOND))

    assert np.all(price[:, -1] > price[:, 0])
    assert np.all(payout[:, -1] < payout[:, 0])


def test_longer_duration_loses_more_to_the_same_rate_rise() -> None:
    """Duration is what orders a bond sleeve against cash, so the ordering is load-bearing:
    a study choosing between a short and an intermediate fund is choosing on exactly this."""

    long_fund = SecuritySymbol("LONG")
    short_fund = SecuritySymbol("SHORT")
    config = _config(
        initial_short_rate=0.01,
        short_rate_mean=0.06,
        instruments=(
            InstrumentSpec(symbol=long_fund, duration_years=12.0),
            InstrumentSpec(symbol=short_fund, duration_years=2.0),
            InstrumentSpec(symbol=CASH, duration_years=0.0),
        ),
    )
    bundle = _sample(config)

    def total_return(symbol: SecuritySymbol) -> float:
        price = _series(bundle, SecurityKey(symbol=symbol))
        return float(price[0, -1] / price[0, 0])

    assert total_return(long_fund) < total_return(short_fund) < total_return(CASH) == 1.0


def test_zero_duration_instrument_is_cash() -> None:
    """A money-market fund: its price never moves and its payout tracks the short rate with no
    lag. That is the definition, and it is what lets the cash sleeve be a holding rather than a
    special case in the engine."""

    bundle = _sample(_rising_rates())
    price = _series(bundle, SecurityKey(symbol=CASH))
    payout = _series(bundle, SecurityDistributionKey(symbol=CASH))

    assert np.allclose(price, price[:, :1])
    # Rising rates, no lag: the payout rises every single month, unlike the bond fund's.
    assert np.all(np.diff(payout, axis=1) > 0.0)


def test_a_funds_book_yield_converges_with_a_half_life_of_its_duration() -> None:
    """The structural claim, stated exactly: a fund earns a new yield only as it rolls into new
    holdings, so its payout converges toward the market yield with a half-life of about its
    duration instead of jumping to it. That lag is what reproduces 2022-2025 — a price that
    fell at once and a payout that took years to climb — rather than a step function.

    `short_rate_mean_reversion=1.0` makes the market yield a STEP: it jumps in month 1 and
    stays. Without that, the market yield is itself still converging and the measured half-life
    would be a blend of the two rates rather than the one being claimed.
    """

    duration_years = 6.0
    half_life_months = round(duration_years * 12)
    # Long enough that the residual gap at the end is ~2^-10 of the jump, so the last month is
    # a fair stand-in for "converged" and the fraction below is not measuring the tail.
    horizon = half_life_months * 10

    bundle = _sample(
        _config(
            initial_short_rate=0.01,
            short_rate_mean=0.06,
            short_rate_mean_reversion=1.0,
            term_spread_mean_reversion=1.0,
            instruments=(InstrumentSpec(symbol=BOND, duration_years=duration_years, initial_price_usd=100.0),),
        ),
        horizon_months=horizon,
    )
    price = _series(bundle, SecurityKey(symbol=BOND), horizon_months=horizon)
    payout = _series(bundle, SecurityDistributionKey(symbol=BOND), horizon_months=horizon)

    # Recovered from the emitted payout alone: a monthly payout per unit over the face it is
    # paid on, annualized. Over the MARK would not recover it — the payout is a coupon on face,
    # which is the thing that does not move when the bonds reprice.
    book_yield = payout * 12.0 / 100.0
    jump = book_yield[0, -1] - book_yield[0, 0]
    assert (book_yield[0, half_life_months] - book_yield[0, 0]) / jump == pytest.approx(0.5, abs=0.01)

    # The price, by contrast, takes the whole hit in the month the yield moves and then sits.
    assert price[0, 1] < price[0, 0]
    assert np.allclose(price[0, 1:], price[0, 1])

    # 2022 in two assertions, and the reason the payout is a coupon on FACE rather than a yield
    # on the mark. In the very month the price takes its whole hit, the payout ticks UP — the
    # opposite direction — and by a small fraction of the move. Paying on the mark would have
    # cut it by the full price drop on the spot, which is not what happened to any real fund.
    price_move = abs(price[0, 1] / price[0, 0] - 1.0)
    payout_move = abs(payout[0, 1] / payout[0, 0] - 1.0)
    assert payout[0, 1] > payout[0, 0]
    assert payout_move < price_move / 5.0


def test_yields_stay_positive_through_a_zirp_decade() -> None:
    """2009-2021 was a real regime, and a zero payout would break the level stack, which is
    multiplicative. The floor is what keeps a ZIRP path from being unsamplable."""

    config = _config(initial_short_rate=0.001, short_rate_mean=0.0)
    bundle = _sample(config)
    for spec in config.instruments:
        payout = _series(bundle, SecurityDistributionKey(symbol=spec.symbol))
        assert np.all(payout >= spec.initial_price_usd * MINIMUM_ANNUAL_YIELD / 12.0)


def test_municipal_spread_lowers_the_pretax_payout() -> None:
    """A muni yields LESS than a Treasury pre-tax — that is the whole reason its exemption is
    worth something. The provider carries the pre-tax price of that; the tax treatment is the
    scenario's business, not the model's."""

    treasury = SecuritySymbol("TREAS")
    muni = SecuritySymbol("MUNI")
    bundle = _sample(
        _config(
            instruments=(
                InstrumentSpec(symbol=treasury, duration_years=6.0, spread=0.0),
                InstrumentSpec(symbol=muni, duration_years=6.0, spread=-0.012),
            )
        )
    )

    treasury_payout = _series(bundle, SecurityDistributionKey(symbol=treasury))
    muni_payout = _series(bundle, SecurityDistributionKey(symbol=muni))
    assert np.all(muni_payout < treasury_payout)


def test_the_rates_coupling_works_when_configured() -> None:
    """`rate_beta` is the only channel between equity and the curve, so the mechanism has to
    work even though the fitted value is zero — a future window, or a different equity proxy,
    could support a nonzero one, and a silently-broken channel would look exactly like the
    honest zero this model ships with."""

    equity = EquitySpec(symbol=EQUITY, initial_price_usd=500.0, monthly_log_return_sigma=0.0, rate_beta=-2.0)
    rising = _series(_sample(_rising_rates().model_copy(update={"equity": equity})), SecurityKey(symbol=EQUITY))
    falling = _series(_sample(_falling_rates().model_copy(update={"equity": equity})), SecurityKey(symbol=EQUITY))

    # Same drift, same (zero) shocks: the only difference between these two paths is the rates
    # state, so a gap proves the channel carries and its sign says a hiking cycle is a headwind.
    assert np.all(rising[:, -1] < falling[:, -1])


def test_equity_ignores_rates_by_default() -> None:
    """The shipped state, asserted rather than left implicit. `rate_beta` fits to +1.57 on
    1993-2026 and -0.62 on 1980-2026, both explaining under half a percent of variance — the
    sign is not stable, so the default is zero and equity is INDEPENDENT of rates here. That is
    a documented gap (SPEC.md), and a model that quietly grew a coupling would invalidate every
    bond/equity conclusion drawn from it without failing anything."""

    equity = EquitySpec(symbol=EQUITY, initial_price_usd=500.0, monthly_log_return_sigma=0.0)
    assert equity.rate_beta == 0.0

    rising = _series(_sample(_rising_rates().model_copy(update={"equity": equity})), SecurityKey(symbol=EQUITY))
    falling = _series(_sample(_falling_rates().model_copy(update={"equity": equity})), SecurityKey(symbol=EQUITY))
    assert np.array_equal(rising, falling)


def test_emissions_are_exactly_the_declared_keys() -> None:
    """`emittable_level_keys` is what sample-sanity and calibration partition against, so a
    provider that advertises a key it does not emit renders as a spurious hard failure, and one
    that emits a key it does not advertise gets silently skipped by every check."""

    config = _config(equity=EquitySpec(symbol=EQUITY, initial_price_usd=500.0))
    model = config.realize_model()
    declared = model.emittable_level_keys()

    assert declared == {
        InflationKey(),
        SecurityKey(symbol=BOND),
        SecurityDistributionKey(symbol=BOND),
        SecurityKey(symbol=CASH),
        SecurityDistributionKey(symbol=CASH),
        # Equity emits a price and no distribution: there is no qualified-dividend income
        # category, so a dividend routed through the interest path would be overtaxed.
        SecurityKey(symbol=EQUITY),
    }
    assert model.emittable_private_equity_issuers() == frozenset()

    request = ExogenousSamplingRequest(
        horizon_months=HORIZON, rollout_seeds=SEEDS, **level_series_request_channels(declared)
    )
    bundle = model.sample(request)
    validate_sample_satisfies_request(request, bundle)
    assert bundle.levels.series_keys() == declared


def test_a_rollout_path_does_not_depend_on_the_batch_it_was_sampled_with() -> None:
    """Per-rollout seeding, the property that lets a caller re-run rollout 7 of 1000 alone."""

    config = StructuralMacroProviderConfig(instruments=(InstrumentSpec(symbol=BOND, duration_years=6.0),))
    model = config.realize_model()

    def path(seeds: tuple[int, ...], index: int) -> np.ndarray:
        bundle = model.sample(ExogenousSamplingRequest(horizon_months=HORIZON, rollout_seeds=seeds))
        matrix = bundle.level_matrix(SecurityKey(symbol=BOND), rollout_count=len(seeds), horizon_months=HORIZON)
        return np.asarray(matrix[index])

    assert np.array_equal(path((5,), 0), path((99, 5, 7), 1))


def test_shocks_actually_move_the_paths() -> None:
    """The deterministic configs above would all still pass against a model that ignored its
    shock inputs entirely."""

    config = StructuralMacroProviderConfig(
        instruments=(InstrumentSpec(symbol=BOND, duration_years=6.0),),
        equity=EquitySpec(symbol=EQUITY, initial_price_usd=500.0),
    )
    bundle = _sample(config)
    for key in (SecurityKey(symbol=BOND), SecurityDistributionKey(symbol=BOND), SecurityKey(symbol=EQUITY)):
        series = _series(bundle, key)
        assert not np.allclose(series[0], series[1])


def test_month_zero_is_the_configured_level() -> None:
    """Anchoring rescales off month 0, so month 0 has to be the level the config states rather
    than one step of drift past it."""

    equity = EquitySpec(symbol=EQUITY, initial_price_usd=500.0)
    bundle = _sample(_config(equity=equity, initial_inflation_level=137.0))

    assert np.all(_series(bundle, SecurityKey(symbol=BOND))[:, 0] == 100.0)
    assert np.all(_series(bundle, SecurityKey(symbol=EQUITY))[:, 0] == 500.0)
    assert np.all(_series(bundle, InflationKey())[:, 0] == 137.0)


def test_a_symbol_priced_twice_is_rejected() -> None:
    """Two rows for one symbol would concatenate into a double-length frame and surface much
    later as a shape error that names the symbol but not the cause."""

    with pytest.raises(ValueError, match="prices a symbol more than once"):
        StructuralMacroProviderConfig(
            instruments=(
                InstrumentSpec(symbol=BOND, duration_years=6.0),
                InstrumentSpec(symbol=BOND, duration_years=2.0),
            )
        ).realize_model()

    with pytest.raises(ValueError, match="prices a symbol more than once"):
        StructuralMacroProviderConfig(
            instruments=(InstrumentSpec(symbol=EQUITY, duration_years=6.0),),
            equity=EquitySpec(symbol=EQUITY, initial_price_usd=1.0),
        ).realize_model()


def test_config_round_trips_through_the_provider_union() -> None:
    """The provider is only reachable from a deployment if the discriminated union dispatches
    on its `type`; being importable is not the same as being configurable."""

    adapter: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)
    parsed = adapter.validate_python(
        {
            "type": "structural_macro",
            "instruments": [{"symbol": "CMF", "duration_years": 5.5, "spread": -0.012}],
            "equity": {"symbol": "VOO", "initial_price_usd": 520.0},
        }
    )
    assert isinstance(parsed, StructuralMacroProviderConfig)
    assert parsed.realize_model().emittable_level_keys() == {
        InflationKey(),
        SecurityKey(symbol=SecuritySymbol("CMF")),
        SecurityDistributionKey(symbol=SecuritySymbol("CMF")),
        SecurityKey(symbol=SecuritySymbol("VOO")),
    }


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InstrumentSpec(symbol=BOND, duration_years=-1.0)


if __name__ == "__main__":
    pytest_bazel.main()
