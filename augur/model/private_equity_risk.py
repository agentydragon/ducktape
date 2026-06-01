"""Prior-parameter private-equity realization-risk sampler."""

from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import Field, model_validator

from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    validate_sample_satisfies_request,
)
from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.schemas import FrozenModel
from augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode
from augur.model.series_model import derive_stream_rollout_seeds

BoolMatrix = npt.NDArray[np.bool_]
CodeMatrix = npt.NDArray[np.int64]
FloatMatrix = npt.NDArray[np.float64]


class PublicMarketCdfAnchor(FrozenModel):
    """One point on the empirical going-public CDF, as a month-index/probability pair.

    `month` is a month index measured from sim start (month 0). The PE risk model is
    calendar-agnostic; the deployment converts prediction-market resolution dates into
    these month offsets. `cumulative_probability` is P(public market opened by `month`),
    so it stays in [0, 1) — a stated certainty (1.0) is disallowed because the tail
    hazard always leaves residual survival mass.
    """

    month: int = Field(ge=1)
    cumulative_probability: float = Field(ge=0.0, lt=1.0)


class ValuationDriftScaleReversion(FrozenModel):
    """Scale-dependent mean-reverting drift for V(t) (M2.2-D).

    The monthly log-drift declines from `mu_young` (when small) toward the issuer's
    `valuation_monthly_log_return_mu` (= `mu_mature`, the asymptote) as the realized log
    enterprise value `s = log V(t)` grows past `log_value_onset_usd`, over a `log_value_scale`
    e-folding in log-value. All four come as a unit (this whole submodel is opt-in), so there is
    no half-set invalid state. `mu_young >= mu_mature` is enforced by the issuer validator (the
    young rate is the hot end; reversion is downward).
    """

    monthly_log_return_mu_young: float = Field(description="Hot drift when the company is small (s <= onset).")
    log_value_onset_usd: float = Field(
        gt=0, description="Log enterprise value at which reversion BEGINS; below it, drift ~ mu_young."
    )
    log_value_scale: float = Field(gt=0, description="e-folding of the excess drift in log-value units past the onset.")


class PrimaryRoundConfig(FrozenModel):
    """Discrete primary-round event stream (mint-streams model, M2.2-C / 2026-06).

    Each round event simultaneously jumps V(t) up by `cash_raised` and dilutes shares
    by an implied amount. Hazard, cash-size, and step-up are all stochastic per rollout.

    Replaces the smooth `annual_dilution_rate` channel when set. The legacy channel is
    still available for the existing `bayesian` preset by leaving this unset; the new
    `bayesian_mint_streams` preset uses this. See augur/plans/mint_streams_model.md.
    """

    monthly_hazard: float = Field(gt=0, le=1.0)
    """Per-month Poisson rate of primary-round arrivals. ~1/18 = 0.056 baseline (one
    round every ~18 months); fit per issuer."""

    monthly_hazard_scale_reversion: ValuationDriftScaleReversion | None = None
    """Optional: `lambda(s) = lambda_mature + (lambda_young - lambda_mature) *
    exp(-max(0, s - onset)/scale)`, `s = log V`. Reuses the same shape submodel as V
    drift; semantics differ — for hazard this models rounds-dry-up as the company
    matures. `monthly_log_return_mu_young` is reinterpreted as `lambda_young`."""

    ipo_anticipation_decay: bool = False
    """If True, multiply the hazard by `(1 - P(public_market_opened_by_t))` so primary
    rounds taper as IPO approaches. Reads from the same `public_market_cdf_anchors` /
    `annual_public_market_probability` as the existing IPO model."""

    cash_over_v_pre_median: float = Field(gt=0)
    """Median round size as fraction of pre-money V(t). e.g. 0.08 ⇒ ~8% raise/V_pre."""

    cash_over_v_pre_log_sigma: float = Field(default=0.5, ge=0)
    """Per-round LogNormal dispersion of cash/V_pre."""

    step_up_median: float = Field(default=1.0, gt=0)
    """Multiplicative info-driven repricing at the round. `V_post = V_pre * (1 +
    cash/V_pre) * step_up`. Default 1.0 is pure mechanical (V_post = V_pre + cash)."""

    step_up_log_sigma: float = Field(default=0.0, ge=0)
    """Per-round LogNormal dispersion of the step-up factor."""


class EmployeeMintConfig(FrozenModel):
    """Continuous employee equity issuance (mint-streams model, M2.2-C / 2026-06).

    `dS_emp/dt = m * S`, smooth exponential between primary rounds. No effect on V
    (SBC is non-cash). Per rollout `m` is drawn LogNormal-around the configured median.
    """

    annual_mint_rate_mature: float = Field(default=0.03, ge=0)
    """Mature-regime per-year employee mint rate. ~3%/yr matches large-cap public tech
    (NVDA / MSFT range). Young-regime companies mint faster; see `scale_reversion`."""

    annual_mint_rate_log_sigma: float = Field(default=0.0, ge=0)
    """Per-rollout LogNormal dispersion of the mint rate. 0.0 ⇒ every rollout uses
    `annual_mint_rate_mature` exactly."""

    scale_reversion: ValuationDriftScaleReversion | None = None
    """Optional: mint rate decays from a hot young rate toward `annual_mint_rate_mature`
    as `log V` grows past onset. `monthly_log_return_mu_young` is reinterpreted as the
    young per-year mint rate (NOT monthly log-drift). Empirically late-stage tech mint
    is fairly stable across maturity, so this is usually unnecessary."""


class PrivateEquityRiskIssuerConfig(FrozenModel):
    """Issuer-level prior parameters for the generic PE realization-risk sampler.

    The runtime artifact is intentionally just numbers. Distribution families live in
    sampler code so later fitting can update the same parameter vector without forcing
    config to carry provenance or distribution tags.
    """

    current_mark_usd: float = Field(gt=0)
    monthly_log_return_mu: float = 0.0
    monthly_log_return_sigma: float = Field(default=0.0, ge=0.0)
    student_t_nu: float = Field(default=5.0, gt=2.0)
    tender_interval_months_median: float = Field(default=12.0, gt=0.0)
    tender_interval_log_sigma: float = Field(default=0.5, ge=0.0)
    tender_price_log_discount_mu: float = 0.0
    tender_price_log_discount_sigma: float = Field(default=0.08, ge=0.0)
    tender_sale_capacity_alpha: float = Field(default=10.0, gt=0.0)
    tender_sale_capacity_beta: float = Field(default=1.0, gt=0.0)
    # Probability that a scheduled tender precursor event is canceled before
    # reaching execution. The realization-risk plan distinguishes the tender
    # opportunity arising (LogNormal-scheduled precursor) from the tender
    # actually executing; cancellation is the gap. Plan [HEURISTIC] baseline
    # 0.08 for normal/neutral macro; macro-state-dependent cancellation is not
    # yet modeled. Independent of the suspension/terminal-state blockers,
    # which deterministically prevent execution.
    tender_cancellation_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    admin_mark_update_interval_months_median: float = Field(default=0.0, ge=0.0)
    admin_mark_update_interval_log_sigma: float = Field(default=0.5, ge=0.0)
    admin_mark_update_log_noise_mu: float = 0.0
    admin_mark_update_log_noise_sigma: float = Field(default=0.10, ge=0.0)
    eligible_fraction: float = Field(default=1.0, ge=0.0, le=1.0)
    # Going-public (IPO) hazard. When `public_market_cdf_anchors` is empty this is a
    # flat constant-per-year hazard for the whole horizon. When anchors are supplied
    # they define a front-loaded, saturating empirical CDF (prediction-market derived)
    # up to the last anchor month, and this annual rate becomes the TAIL hazard applied
    # to every month past the last anchor. Existing anchor-free configs are unchanged.
    annual_public_market_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    # Empirical going-public CDF anchors as (month-from-sim-start, P(public by month))
    # pairs. Between consecutive anchors a constant monthly hazard reproduces the CDF
    # exactly; past the last anchor the flat `annual_public_market_probability` tail
    # takes over. Empty => the legacy flat-hazard behaviour.
    public_market_cdf_anchors: tuple[PublicMarketCdfAnchor, ...] = ()
    public_market_lockup_months: int = Field(default=0, ge=0)
    public_market_price_log_discount_mu: float = 0.0
    public_market_price_log_discount_sigma: float = Field(default=0.20, ge=0.0)
    annual_liquidity_suspension_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    liquidity_suspension_months_min: int = Field(default=1, ge=1)
    liquidity_suspension_months_max: int = Field(default=6, ge=1)
    # Total annual rate of legal/administrative shock events. The shock's effect on
    # the issuer follows the realization-risk plan's three-way severity split (see
    # gaffer-private/gaffer_augur/openai_stock/realization_risk_model_plan.md
    # §Liquidity And Legal Execution Shocks): 80% temporary recoverable block, 15%
    # permanent sale-capacity cap, 5% severe `LEGAL_IMPAIRMENT` event. The 5% severe
    # case is further split 50/30/20 between indefinite block, near-zero permanent
    # capacity, and small-dollar forced recovery. Only the 5% severe branch emits a
    # `LEGAL_IMPAIRMENT` event_kind marker; the 80% temporary and 15% permanent
    # branches affect protocol channels (liquidity_blocked, sale_capacity_fraction)
    # without a discrete event marker.
    annual_legal_event_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    annual_forced_sale_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    forced_sale_fraction_alpha: float = Field(default=1.0, gt=0.0)
    forced_sale_fraction_beta: float = Field(default=1.0, gt=0.0)
    annual_forced_recovery_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    forced_recovery_cashout_usd_min: float = Field(default=0.0, ge=0.0)
    forced_recovery_cashout_usd_max: float = Field(default=0.0, ge=0.0)
    annual_collapse_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    collapsed_mark_fraction: float = Field(default=0.0, ge=0.0, le=1.0)

    # -- M2 coupled valuation + dilution channel (opt-in) --------------------
    #
    # When `current_valuation_usd` is set, the per-unit latent mark stops being
    # a standalone Student-t random walk and instead becomes a quantity DERIVED
    # from a sampled company valuation V(t) and a deterministic dilution factor:
    #
    #     latent_mark(t) = current_mark_usd * (V(t) / V0) / dilution_factor(t)
    #
    # Leaving `current_valuation_usd` unset keeps today's independent latent-mark
    # random walk byte-for-byte (zero-value regression for existing configs).
    current_valuation_usd: float | None = Field(default=None, gt=0)
    """Company market-cap anchor `V0` (USD).

    `None` disables the valuation channel AND selects the legacy independent
    latent-mark random walk. Must be set together with
    `shares_outstanding_initial`.
    """
    valuation_monthly_log_return_mu: float = 0.0
    """Monthly log-space drift of V(t). Only meaningful when the channel is on.

    With `valuation_drift_scale_reversion` unset this is the CONSTANT monthly drift (legacy
    behavior). With it set, this is the LONG-RUN MATURE drift `mu_mature` that the
    scale-dependent drift reverts toward as the company grows large.
    """
    valuation_drift_scale_reversion: ValuationDriftScaleReversion | None = None
    """Optional scale-dependent mean-reverting drift (M2.2-D).

    When set, the monthly drift is a function of the realized company SIZE `s = log V(t)`
    rather than a constant:

        mu_V(s) = mu_mature + (mu_young - mu_mature) * exp(-max(0, s - s_onset) / s_scale)

    with `mu_mature = valuation_monthly_log_return_mu`. A small company (`s <= s_onset`) grows
    at `mu_young`; a large one reverts toward `mu_mature`, and keeps maturing as it grows. This
    makes V(t) a genuine SDE (drift depends on the realized level), so the sampler integrates it
    month by month. It is data-driven (the boom is tamed because the company is observably large
    NOW, not by a calendar prior) and self-correcting per rollout. `None` => constant drift,
    byte-identical to the legacy single-drift path. See augur/plans/prediction_market_calibration.md
    (M2.2-D) for the empirical grounding (firm-growth scaling laws; conservative mu_mature ~
    S&P 100-yr CAGR).
    """
    valuation_monthly_log_return_sigma: float = Field(default=0.0, ge=0.0)
    """Monthly log-space volatility of V(t). Only meaningful when the channel is on."""
    valuation_student_t_nu: float = Field(default=5.0, gt=2.0)
    """Degrees of freedom for V(t)'s Student-t shocks. Only meaningful when the channel is on."""
    shares_outstanding_initial: float | None = Field(default=None, gt=0)
    """Initial share count `shares0`.

    Required together with `current_valuation_usd`. In v1 only the *ratio*
    `shares(t)/shares0` enters the mark via the dilution factor, so V0 and
    shares0 cancel at t=0; we still require it set so the process is honestly
    specified for the Bayesian fit (M2.2 / TODO #1734).
    """
    annual_dilution_rate: float = Field(default=0.0, ge=0.0)
    """Continuous per-year share-count growth (employee mint + baseline).

    The per-rollout MEDIAN dilution rate: each rollout draws
    `r = annual_dilution_rate * exp(annual_dilution_rate_log_sigma * z)`, `z ~ N(0, 1)`,
    driving `dilution_factor(t) = (1 + r) ** (t / 12)`. The primary-round / secondary-trade
    distinction (M2.2-C) and the full Bayesian posterior (M2.2-D) are DEFERRED. Only
    meaningful when the valuation channel is on.
    """
    annual_dilution_rate_log_sigma: float = Field(default=0.0, ge=0.0)
    """Per-rollout dilution-rate dispersion (M2.2-A).

    Each rollout draws `r = annual_dilution_rate * exp(annual_dilution_rate_log_sigma * z)`
    with `z ~ N(0, 1)` -- a **median-anchored** LogNormal: `median(r) == annual_dilution_rate`
    exactly (a LogNormal's median is `exp(mu)`). This is deliberately NOT mean-anchored --
    mean-anchoring would inflate the typical realized dilution by `exp(sigma**2 / 2)` as
    sigma grows, biasing the central mark path. Default `0.0` => every rollout gets exactly
    `annual_dilution_rate` (since `exp(0) == 1`), byte-identical to the M2 deterministic
    factor. Only meaningful when the valuation channel is on; left unguarded in the validator
    since it defaults inert and is simply ignored when the channel is off.
    """

    # -- M2.2-C mint-streams channel (opt-in via primary_round_config) -------
    #
    # When `primary_round_config` is set, the smooth `(1 + r)^(t/12)` dilution path is
    # replaced by:
    #   shares(t) = shares0 * mint_factor(t) * product over rounds k <= t of
    #              (1 + cash_over_v_pre_k / step_up_k)
    # and V(t) jumps at round events:
    #   V(t_k+) = V(t_k-) * (1 + cash_over_v_pre_k) * step_up_k
    # with V between events still integrated as the scale-reverting Student-t SDE.
    # `latent_mark(t) = current_mark_usd * (V(t)/V0) / (shares(t)/shares0)` — same
    # algebraic structure as the legacy formula with `dilution_factor = shares(t)/shares0`.
    primary_round_config: PrimaryRoundConfig | None = None
    """Discrete primary-round event stream. See `PrimaryRoundConfig`. Must be set
    together with `employee_mint_config`; both require `current_valuation_usd` +
    `shares_outstanding_initial`. When set, legacy `annual_dilution_rate` must be 0."""

    employee_mint_config: EmployeeMintConfig | None = None
    """Continuous employee-mint stream. See `EmployeeMintConfig`. Must be set together
    with `primary_round_config`."""

    @model_validator(mode="after")
    def _validate_ranges(self) -> PrivateEquityRiskIssuerConfig:
        if self.liquidity_suspension_months_max < self.liquidity_suspension_months_min:
            raise ValueError("liquidity_suspension_months_max must be >= min")
        if self.forced_recovery_cashout_usd_max < self.forced_recovery_cashout_usd_min:
            raise ValueError("forced_recovery_cashout_usd_max must be >= min")
        months = [anchor.month for anchor in self.public_market_cdf_anchors]
        if any(later <= earlier for earlier, later in itertools.pairwise(months)):
            raise ValueError("public_market_cdf_anchors must be strictly increasing in month")
        cumulatives = [anchor.cumulative_probability for anchor in self.public_market_cdf_anchors]
        if any(later < earlier for earlier, later in itertools.pairwise(cumulatives)):
            raise ValueError("public_market_cdf_anchors must be non-decreasing in cumulative_probability")
        # `current_valuation_usd` (V0) and `shares_outstanding_initial` (shares0)
        # must be set together or both left unset: you need both to form the
        # coupled mark honestly. `annual_dilution_rate` and the valuation RW
        # params are only meaningful when the channel is on; they default to
        # inert values and are simply ignored when it is off, so they are left
        # deliberately unguarded.
        if (self.current_valuation_usd is None) != (self.shares_outstanding_initial is None):
            raise ValueError("current_valuation_usd and shares_outstanding_initial must be set together or both unset")
        # Scale-reversion (M2.2-D) reverts the drift DOWNWARD from a hot young rate toward the
        # mature asymptote `valuation_monthly_log_return_mu`, so the young rate must be the higher
        # end. (Equality is allowed: it degenerates to constant drift.)
        reversion = self.valuation_drift_scale_reversion
        if reversion is not None and reversion.monthly_log_return_mu_young < self.valuation_monthly_log_return_mu:
            raise ValueError(
                "valuation_drift_scale_reversion.monthly_log_return_mu_young must be >= the mature "
                "valuation_monthly_log_return_mu (reversion is downward toward maturity)"
            )
        # Mint-streams channel (M2.2-C): the two configs are a unit (need both to model
        # share count honestly) and require the valuation channel to be on.
        if (self.primary_round_config is None) != (self.employee_mint_config is None):
            raise ValueError("primary_round_config and employee_mint_config must be set together or both unset")
        if self.primary_round_config is not None:
            if self.current_valuation_usd is None:
                raise ValueError("mint-streams channel requires current_valuation_usd + shares_outstanding_initial")
            # The mint-streams channel REPLACES the smooth `(1+r)^(t/12)` dilution path.
            # Allowing both at once would silently double-count dilution; require legacy off.
            if self.annual_dilution_rate != 0.0 or self.annual_dilution_rate_log_sigma != 0.0:
                raise ValueError(
                    "annual_dilution_rate (legacy smooth channel) must be 0 when primary_round_config is set"
                )
        return self

    @property
    def valuation_channel_enabled(self) -> bool:
        """Whether the opt-in coupled valuation + dilution channel is active."""

        return self.current_valuation_usd is not None

    @property
    def mint_streams_channel_enabled(self) -> bool:
        """Whether the M2.2-C mint-streams (discrete rounds + employee mint) channel is active."""

        return self.primary_round_config is not None


class PrivateEquityRiskProviderConfig(FrozenModel):
    type: Literal["private_equity_risk"] = "private_equity_risk"
    issuers: dict[str, PrivateEquityRiskIssuerConfig] = Field(min_length=1)

    def realize_model(self) -> PrivateEquityRiskModel:
        return PrivateEquityRiskModel(issuers=self.issuers)


@dataclass(frozen=True)
class PrivateEquityRiskModel:
    issuers: dict[str, PrivateEquityRiskIssuerConfig]
    label: str = "private_equity_risk"

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        rollout_count = request.rollout_count
        horizon_months = request.horizon_months
        pe_bundle_parts: list[PrivateEquityBundle] = []
        prices: dict[str, float] = {}
        for issuer_id, issuer in sorted(self.issuers.items()):
            paths = _sample_issuer(issuer_id, issuer, request)
            prices[issuer_id] = issuer.current_mark_usd
            pe_bundle_parts.append(
                PrivateEquityBundle.from_issuer_arrays(
                    issuer_id,
                    mark_usd_per_unit=paths.mark.astype(np.float64),
                    regime_code=paths.regime_code.astype(np.int64),
                    event_kind_code=paths.event_kind_code.astype(np.int64),
                    sale_opportunity_active=paths.tender_events.astype(np.bool_),
                    sale_capacity_fraction=paths.sale_capacity_fraction.astype(np.float64),
                    eligible_fraction=paths.eligible_fraction.astype(np.float64),
                    forced_sale_fraction=paths.forced_sale_fraction.astype(np.float64),
                    liquidity_blocked=(paths.liquidity_blocked >= 0.5).astype(np.bool_),
                    forced_recovery_cashout_usd=paths.forced_recovery_cashout_usd.astype(np.float64),
                    company_valuation_usd=paths.company_valuation_usd.astype(np.float64),
                    rollout_count=rollout_count,
                    horizon_months=horizon_months,
                )
            )

        sampled = SampledExogenousBundle(
            levels=SERIES_LEVELS_SCHEMA.to_frame(),
            private_equity=PrivateEquityBundle.combine(pe_bundle_parts),
            metadata={
                "model_id": self.label,
                "private_equity_issuers": tuple(sorted(self.issuers)),
                "private_equity_prices_usd": prices,
            },
        )
        validate_sample_satisfies_request(request, sampled)
        return sampled


@dataclass(frozen=True)
class _IssuerPaths:
    mark: FloatMatrix
    tender_events: BoolMatrix
    event_kind_code: CodeMatrix
    regime_code: CodeMatrix
    sale_capacity_fraction: FloatMatrix
    eligible_fraction: FloatMatrix
    forced_sale_fraction: FloatMatrix
    liquidity_blocked: FloatMatrix
    forced_recovery_cashout_usd: FloatMatrix
    # Company market cap V(t), (R, T+1). All-zeros when the valuation channel
    # is off (no `current_valuation_usd` anchor).
    company_valuation_usd: FloatMatrix


# Plan-derived constants (realization_risk_model_plan.md §Liquidity And Legal Execution Shocks).
# These split a single legal-event arrival into the three structural severities.
_LEGAL_TEMPORARY_SHARE = 0.80  # recoverable temporary block, no event_kind marker
_LEGAL_PERMANENT_CAP_SHARE = 0.15  # permanent sale-capacity cap, no event_kind marker
# Severe share is 1 - 0.80 - 0.15 = 0.05 — only path that emits LEGAL_IMPAIRMENT.

# Sub-mechanism split within the 5% severe case.
_LEGAL_SEVERE_INDEFINITE_SHARE = 0.50  # indefinite liquidity block
_LEGAL_SEVERE_NEAR_ZERO_CAP_SHARE = 0.30  # near-zero permanent sale capacity
# Small-dollar forced recovery share is 1 - 0.50 - 0.30 = 0.20.

_LEGAL_TEMPORARY_MONTHLY_RECOVERY_PROBABILITY = 0.12  # geometric cure rate per month
_LEGAL_PERMANENT_CAPACITY_MAX_FACTOR = 0.20  # uniform [0, 0.20]
_LEGAL_SEVERE_NEAR_ZERO_CAPACITY_LO = 0.000001  # uniform [1e-6, 1e-3]
_LEGAL_SEVERE_NEAR_ZERO_CAPACITY_HI = 0.001
_LEGAL_SEVERE_SMALL_DOLLAR_USD_MAX = 100_000.0  # uniform [0, 100_000]


def _sample_issuer(
    issuer_id: str, issuer: PrivateEquityRiskIssuerConfig, request: ExogenousSamplingRequest
) -> _IssuerPaths:
    """Vectorized sampler: all R rollouts evolve in parallel, one timestep at a time.

    Per-month state mutations use boolean masks over the rollout axis so each branch
    fires for many rollouts in a single numpy call. The order of branches within a
    month preserves the priority structure of the original per-rollout code path
    (forced_recovery → collapse → public_market_open → legal_event → suspension →
    forced_sale → tender → admin_mark_update).
    """

    rollout_count = request.rollout_count
    horizon_months = request.horizon_months
    shape = (rollout_count, horizon_months + 1)

    # State arrays — all (R, T+1).
    mark = np.full(shape, issuer.current_mark_usd, dtype=np.float64)
    tender_events = np.zeros(shape, dtype=np.bool_)
    event_kind_code = np.zeros(shape, dtype=np.int64)
    regime_code = np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64)
    sale_capacity_fraction = np.zeros(shape, dtype=np.float64)
    forced_sale_fraction = np.zeros(shape, dtype=np.float64)
    liquidity_blocked = np.zeros(shape, dtype=np.float64)
    forced_recovery_cashout_usd = np.zeros(shape, dtype=np.float64)

    # Per-rollout cumulative state (R,).
    collapsed = np.zeros(rollout_count, dtype=np.bool_)
    acquired = np.zeros(rollout_count, dtype=np.bool_)
    public_market = np.zeros(rollout_count, dtype=np.bool_)
    suspended_through = np.full(rollout_count, -1, dtype=np.int64)
    public_market_lockup_through = np.full(rollout_count, -1, dtype=np.int64)
    # Permanent sale-capacity cap from legal-event 15% permanent branch and 5% severe
    # near-zero sub-branch. Multiplies tender sale_capacity_fraction at issue time.
    permanent_capacity_cap = np.ones(rollout_count, dtype=np.float64)

    level_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_level")
    event_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_event")

    # M2 coupled valuation + dilution channel (opt-in via `current_valuation_usd`).
    #
    # Channel ON: V(t) is the primitive (its OWN derived seed stream, independent
    # of the latent-mark/event streams), and the per-unit latent mark is DERIVED
    #   latent_mark(t) = current_mark_usd * (V(t)/V0) / dilution_factor(t).
    # Because only ratios enter, V0 and shares0 cancel at t=0, so latent_mark[:,0]
    # == current_mark_usd exactly. The event-noisy marks below already read
    # `latent_mark[branch, t]`, so tender/admin/public-market/forced-sale noise
    # composes on TOP of the coupled mark unchanged.
    #
    # Channel OFF: the legacy independent Student-t latent-mark walk is used
    # verbatim and the valuation path is all-zeros. The valuation seed stream is
    # NOT derived in this branch, so neither the level nor the event RNG stream
    # is perturbed — the mark/event arrays stay byte-identical to pre-M2.
    # Direct `is not None` check (equivalent to `issuer.valuation_channel_enabled`) so
    # mypy narrows `current_valuation_usd` to `float` for the division/log below.
    if issuer.mint_streams_channel_enabled:
        # M2.2-C: event-driven primary rounds + continuous employee mint. V jumps at
        # round events; shares are a smooth mint exponential plus round-event jumps.
        # `_sample_company_valuation_and_shares_with_rounds_vectorized` returns both.
        valuation_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_valuation")
        round_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_rounds")
        mint_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_mint")
        company_valuation_usd, shares_path = _sample_company_valuation_and_shares_with_rounds_vectorized(
            issuer,
            valuation_seeds=valuation_seeds,
            round_seeds=round_seeds,
            mint_seeds=mint_seeds,
            horizon_months=horizon_months,
        )
        # Caller only enters this branch when the mint-streams channel is enabled, which the
        # validator forces alongside the valuation channel — so V0 and shares0 are guaranteed set.
        assert issuer.current_valuation_usd is not None
        assert issuer.shares_outstanding_initial is not None
        latent_mark = (
            issuer.current_mark_usd
            * (company_valuation_usd / issuer.current_valuation_usd)
            / (shares_path / issuer.shares_outstanding_initial)
        )
    elif issuer.current_valuation_usd is not None:
        valuation_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:pe_risk_valuation")
        company_valuation_usd = _sample_company_valuation_vectorized(
            issuer, valuation_seeds=valuation_seeds, horizon_months=horizon_months
        )
        # Per-rollout dilution factor (R, T+1). ONE unified path: each rollout draws its
        # own rate `r` off the INDEPENDENT `:pe_risk_dilution` seed stream, so the dilution
        # draw cannot perturb the level/event/valuation RNG streams. With the default
        # annual_dilution_rate_log_sigma == 0 every z collapses via exp(0) == 1, so r equals
        # annual_dilution_rate for every rollout and each row is byte-identical to the M2
        # deterministic `(1 + rate) ** (t / 12)` factor -- no sigma==0 special-case needed.
        dilution = _dilution_factor(
            annual_dilution_rate=issuer.annual_dilution_rate,
            annual_dilution_rate_log_sigma=issuer.annual_dilution_rate_log_sigma,
            rollout_seeds=request.rollout_seeds,
            issuer_id=issuer_id,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
        )
        latent_mark = issuer.current_mark_usd * (company_valuation_usd / issuer.current_valuation_usd) / dilution
    else:
        latent_mark = _sample_latent_marks_vectorized(issuer, level_seeds=level_seeds, horizon_months=horizon_months)
        company_valuation_usd = np.zeros(shape, dtype=np.float64)

    # Per-rollout deterministic event-month masks ((R, T+1) booleans). One Generator
    # seeded by the hash of all per-rollout event seeds; vectorization gives up the
    # original property that rollout R's draws depend only on its own seed.
    event_rng = np.random.default_rng(_seed_from_rollout_seeds(event_seeds))
    # Tender-precursor schedule. The precursor event firing is necessary but not
    # sufficient for an actual tender — execution is gated below by the
    # cancellation draw and by the eligibility mask (terminal/suspended state).
    tender_scheduled_mask = _sample_event_month_mask_vectorized(
        median_months=issuer.tender_interval_months_median,
        log_sigma=issuer.tender_interval_log_sigma,
        rng=event_rng,
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    admin_mask = _sample_event_month_mask_vectorized(
        median_months=issuer.admin_mark_update_interval_months_median,
        log_sigma=issuer.admin_mark_update_interval_log_sigma,
        rng=event_rng,
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )

    # Pre-sampled uniform draws for stochastic branch decisions ((R, T) floats in [0, 1)).
    # Indexed by u_*[r, t - 1] in the per-month loop.
    u_recovery = event_rng.random((rollout_count, horizon_months))
    u_collapse = event_rng.random((rollout_count, horizon_months))
    u_public = event_rng.random((rollout_count, horizon_months))
    u_legal = event_rng.random((rollout_count, horizon_months))
    u_legal_severity = event_rng.random((rollout_count, horizon_months))
    u_legal_severe_mechanism = event_rng.random((rollout_count, horizon_months))
    u_suspension = event_rng.random((rollout_count, horizon_months))
    u_forced_sale = event_rng.random((rollout_count, horizon_months))
    u_tender_cancellation = event_rng.random((rollout_count, horizon_months))

    monthly_public_open = _public_market_open_hazard_by_month(issuer, horizon_months)
    monthly_suspension = _monthly_probability(issuer.annual_liquidity_suspension_probability)
    monthly_forced_sale = _monthly_probability(issuer.annual_forced_sale_probability)
    monthly_recovery = _monthly_probability(issuer.annual_forced_recovery_probability)
    monthly_collapse = _monthly_probability(issuer.annual_collapse_probability)
    monthly_legal = _monthly_probability(issuer.annual_legal_event_probability)

    def _apply_terminal_block(mask: np.ndarray, *, regime_value: int, mark_factor: float | None, t: int) -> None:
        """Forward-fill regime + liquidity_blocked for rollouts in `mask` from month t onward.
        If `mark_factor` is set, the mark from t onward becomes mark[:, t-1] * mark_factor.
        """
        if not mask.any():
            return
        rows = np.where(mask)[0]
        future_cols = np.arange(t, horizon_months + 1)
        regime_code[np.ix_(rows, future_cols)] = regime_value
        liquidity_blocked[np.ix_(rows, future_cols)] = 1.0
        if mark_factor is not None:
            new_mark = mark[rows, t - 1] * mark_factor
            mark[np.ix_(rows, future_cols)] = new_mark[:, None]

    for t in range(1, horizon_months + 1):
        u_idx = t - 1
        # Default mark = mark[t-1]; overridden below as needed.
        mark[:, t] = mark[:, t - 1]

        # Carry terminal/in-progress state.
        # Public-market rollouts: mark follows latent_mark; regime = PUBLIC_MARKET; liquidity_blocked during lockup.
        if public_market.any():
            mark[public_market, t] = latent_mark[public_market, t]
            regime_code[public_market, t] = int(PrivateEquityRegimeCode.PUBLIC_MARKET)
            in_lockup = public_market & (t <= public_market_lockup_through)
            liquidity_blocked[in_lockup, t] = 1.0
            sale_capacity_fraction[public_market & ~in_lockup, t] = 1.0

        # Ordinary liquidity suspension (no event_kind, just a model-internal block).
        active_now = ~(collapsed | acquired | public_market)
        suspended_now = active_now & (t <= suspended_through)
        if suspended_now.any():
            regime_code[suspended_now, t] = int(PrivateEquityRegimeCode.PRIVATE_OPERATING)
            liquidity_blocked[suspended_now, t] = 1.0

        # Eligible for new events.
        eligible = active_now & ~suspended_now

        if eligible.any():
            # Branch 1: forced_recovery → COLLAPSED with low-dollar cashout.
            branch = eligible & (u_recovery[:, u_idx] < monthly_recovery)
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.FORCED_RECOVERY)
                n = int(branch.sum())
                forced_recovery_cashout_usd[branch, t] = event_rng.uniform(
                    issuer.forced_recovery_cashout_usd_min, issuer.forced_recovery_cashout_usd_max, size=n
                )
                _apply_terminal_block(
                    branch,
                    regime_value=int(PrivateEquityRegimeCode.COLLAPSED),
                    mark_factor=issuer.collapsed_mark_fraction,
                    t=t,
                )
                collapsed |= branch
                eligible &= ~branch

            # Branch 2: collapse → COLLAPSED.
            branch = eligible & (u_collapse[:, u_idx] < monthly_collapse)
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.COLLAPSE)
                _apply_terminal_block(
                    branch,
                    regime_value=int(PrivateEquityRegimeCode.COLLAPSED),
                    mark_factor=issuer.collapsed_mark_fraction,
                    t=t,
                )
                collapsed |= branch
                eligible &= ~branch

            # Branch 3: public_market_open. The per-month hazard is the empirical
            # IPO-prior CDF inside the anchored window and the flat tail past it.
            branch = eligible & (u_public[:, u_idx] < monthly_public_open[t])
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
                # Forward-fill PUBLIC_MARKET regime from t onward.
                rows = np.where(branch)[0]
                future_cols = np.arange(t, horizon_months + 1)
                regime_code[np.ix_(rows, future_cols)] = int(PrivateEquityRegimeCode.PUBLIC_MARKET)
                # Noisy public-market open mark at t.
                mark[branch, t] = _noisy_mark_vectorized(
                    latent_mark[branch, t],
                    rng=event_rng,
                    log_noise_mu=issuer.public_market_price_log_discount_mu,
                    log_noise_sigma=issuer.public_market_price_log_discount_sigma,
                )
                new_lockup_through = min(horizon_months, t + issuer.public_market_lockup_months - 1)
                public_market_lockup_through[branch] = new_lockup_through
                # Within-lockup liquidity_blocked.
                if new_lockup_through >= t:
                    lockup_cols = np.arange(t, new_lockup_through + 1)
                    liquidity_blocked[np.ix_(rows, lockup_cols)] = 1.0
                public_market |= branch
                eligible &= ~branch

            # Branch 4: legal_event (umbrella) — 80% temporary recoverable / 15% permanent cap / 5% severe.
            branch = eligible & (u_legal[:, u_idx] < monthly_legal)
            if branch.any():
                sev = u_legal_severity[:, u_idx]
                temp_branch = branch & (sev < _LEGAL_TEMPORARY_SHARE)
                perm_branch = (
                    branch
                    & (sev >= _LEGAL_TEMPORARY_SHARE)
                    & (sev < _LEGAL_TEMPORARY_SHARE + _LEGAL_PERMANENT_CAP_SHARE)
                )
                severe_branch = branch & (sev >= _LEGAL_TEMPORARY_SHARE + _LEGAL_PERMANENT_CAP_SHARE)

                # 80% temporary: stochastic-duration liquidity block via Geometric(p=0.12).
                # No event_kind marker (per plan).
                if temp_branch.any():
                    n = int(temp_branch.sum())
                    durations = event_rng.geometric(p=_LEGAL_TEMPORARY_MONTHLY_RECOVERY_PROBABILITY, size=n).astype(
                        np.int64
                    )
                    new_through = np.minimum(horizon_months, t + durations - 1)
                    suspended_through[temp_branch] = np.maximum(suspended_through[temp_branch], new_through)

                # 15% permanent cap: reduce permanent_capacity_cap by Uniform[0, 0.20] cap.
                # No event_kind marker (per plan).
                if perm_branch.any():
                    n = int(perm_branch.sum())
                    new_cap = event_rng.uniform(0.0, _LEGAL_PERMANENT_CAPACITY_MAX_FACTOR, size=n)
                    permanent_capacity_cap[perm_branch] = np.minimum(permanent_capacity_cap[perm_branch], new_cap)

                # 5% severe: emit LEGAL_IMPAIRMENT + pick a severity sub-mechanism.
                if severe_branch.any():
                    event_kind_code[severe_branch, t] = int(PrivateEquityEventKindCode.LEGAL_IMPAIRMENT)
                    sub = u_legal_severe_mechanism[:, u_idx]
                    indef = severe_branch & (sub < _LEGAL_SEVERE_INDEFINITE_SHARE)
                    near_zero = (
                        severe_branch
                        & (sub >= _LEGAL_SEVERE_INDEFINITE_SHARE)
                        & (sub < _LEGAL_SEVERE_INDEFINITE_SHARE + _LEGAL_SEVERE_NEAR_ZERO_CAP_SHARE)
                    )
                    small_recovery = severe_branch & (
                        sub >= _LEGAL_SEVERE_INDEFINITE_SHARE + _LEGAL_SEVERE_NEAR_ZERO_CAP_SHARE
                    )

                    # 50%: indefinite liquidity block.
                    if indef.any():
                        rows = np.where(indef)[0]
                        future_cols = np.arange(t, horizon_months + 1)
                        liquidity_blocked[np.ix_(rows, future_cols)] = 1.0
                        suspended_through[indef] = horizon_months

                    # 30%: near-zero permanent sale capacity.
                    if near_zero.any():
                        n = int(near_zero.sum())
                        new_cap = event_rng.uniform(
                            _LEGAL_SEVERE_NEAR_ZERO_CAPACITY_LO, _LEGAL_SEVERE_NEAR_ZERO_CAPACITY_HI, size=n
                        )
                        permanent_capacity_cap[near_zero] = np.minimum(permanent_capacity_cap[near_zero], new_cap)

                    # 20%: small-dollar forced recovery (records cashout without collapsing).
                    if small_recovery.any():
                        n = int(small_recovery.sum())
                        forced_recovery_cashout_usd[small_recovery, t] = event_rng.uniform(
                            0.0, _LEGAL_SEVERE_SMALL_DOLLAR_USD_MAX, size=n
                        )

                eligible &= ~branch

            # Branch 5: ordinary suspension (no event_kind per plan).
            branch = eligible & (u_suspension[:, u_idx] < monthly_suspension)
            if branch.any():
                n = int(branch.sum())
                durations = event_rng.integers(
                    low=issuer.liquidity_suspension_months_min, high=issuer.liquidity_suspension_months_max + 1, size=n
                ).astype(np.int64)
                new_through = np.minimum(horizon_months, t + durations - 1)
                suspended_through[branch] = np.maximum(suspended_through[branch], new_through)
                eligible &= ~branch

            # Branch 6: forced_sale → ACQUIRED.
            branch = eligible & (u_forced_sale[:, u_idx] < monthly_forced_sale)
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.ACQUISITION_CASHOUT)
                n = int(branch.sum())
                forced_sale_fraction[branch, t] = event_rng.beta(
                    issuer.forced_sale_fraction_alpha, issuer.forced_sale_fraction_beta, size=n
                )
                mark[branch, t] = _noisy_mark_vectorized(
                    latent_mark[branch, t],
                    rng=event_rng,
                    log_noise_mu=issuer.public_market_price_log_discount_mu,
                    log_noise_sigma=issuer.public_market_price_log_discount_sigma,
                )
                # Forward-fill ACQUIRED regime + liquidity_blocked from t (the firing
                # month) onward; the mark stays at the noisy_mark value set above for
                # t and carries forward (the carry-forward at the top of the next
                # iteration uses mark[:, t-1], so we set mark[:, t+1:] explicitly).
                rows = np.where(branch)[0]
                future_cols = np.arange(t, horizon_months + 1)
                regime_code[np.ix_(rows, future_cols)] = int(PrivateEquityRegimeCode.ACQUIRED)
                liquidity_blocked[np.ix_(rows, future_cols)] = 1.0
                if t < horizon_months:
                    later_cols = np.arange(t + 1, horizon_months + 1)
                    mark[np.ix_(rows, later_cols)] = mark[rows, t][:, None]
                acquired |= branch
                eligible &= ~branch

            # Branch 7: tender. A tender executes only when the scheduled precursor
            # fires AND the cancellation draw misses AND no blocker preempted the
            # month (eligibility already rules out terminal/suspended state).
            tender_precursor_fires = tender_scheduled_mask[:, t]
            tender_canceled = u_tender_cancellation[:, u_idx] < issuer.tender_cancellation_probability
            branch = eligible & tender_precursor_fires & ~tender_canceled
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.TENDER)
                tender_events[branch, t] = True
                n = int(branch.sum())
                raw_capacity = event_rng.beta(
                    issuer.tender_sale_capacity_alpha, issuer.tender_sale_capacity_beta, size=n
                )
                sale_capacity_fraction[branch, t] = raw_capacity * permanent_capacity_cap[branch]
                mark[branch, t] = _noisy_mark_vectorized(
                    latent_mark[branch, t],
                    rng=event_rng,
                    log_noise_mu=issuer.tender_price_log_discount_mu,
                    log_noise_sigma=issuer.tender_price_log_discount_sigma,
                )
                eligible &= ~branch

            # Branch 8: admin_mark_update (deterministic schedule, gated by eligibility).
            branch = eligible & admin_mask[:, t]
            if branch.any():
                event_kind_code[branch, t] = int(PrivateEquityEventKindCode.ADMIN_MARK_UPDATE)
                mark[branch, t] = _noisy_mark_vectorized(
                    latent_mark[branch, t],
                    rng=event_rng,
                    log_noise_mu=issuer.admin_mark_update_log_noise_mu,
                    log_noise_sigma=issuer.admin_mark_update_log_noise_sigma,
                )
                eligible &= ~branch

    eligible_fraction = np.full(shape, issuer.eligible_fraction, dtype=np.float64)

    return _IssuerPaths(
        mark=mark,
        tender_events=tender_events,
        event_kind_code=event_kind_code,
        regime_code=regime_code,
        sale_capacity_fraction=sale_capacity_fraction,
        eligible_fraction=eligible_fraction,
        forced_sale_fraction=forced_sale_fraction,
        liquidity_blocked=liquidity_blocked,
        forced_recovery_cashout_usd=forced_recovery_cashout_usd,
        company_valuation_usd=company_valuation_usd,
    )


def _sample_latent_marks_vectorized(
    issuer: PrivateEquityRiskIssuerConfig, *, level_seeds: tuple[int, ...], horizon_months: int
) -> FloatMatrix:
    """Vectorized latent mark sampler: returns (R, horizon_months + 1)."""

    rollout_count = len(level_seeds)
    paths = np.empty((rollout_count, horizon_months + 1), dtype=np.float64)
    paths[:, 0] = issuer.current_mark_usd
    if horizon_months == 0:
        return paths
    # Single Generator seeded by the hash of all per-rollout level seeds; vectorized
    # sampling forfeits the original per-rollout-seed independence but stays
    # deterministic given a fixed sequence of seeds.
    rng = np.random.default_rng(_seed_from_rollout_seeds(level_seeds))
    shocks = rng.standard_t(df=issuer.student_t_nu, size=(rollout_count, horizon_months))
    shocks *= issuer.monthly_log_return_sigma
    log_path = math.log(issuer.current_mark_usd) + np.cumsum(issuer.monthly_log_return_mu + shocks, axis=1)
    try:
        with np.errstate(over="raise", invalid="raise"):
            paths[:, 1:] = np.exp(log_path)
    except FloatingPointError as error:
        raise ValueError("private-equity risk model produced non-finite marks") from error
    if not np.all(np.isfinite(paths)) or np.any(paths <= 0.0):
        raise ValueError("private-equity risk model produced invalid marks")
    return paths


def _scale_reverting_drift(
    log_value: FloatMatrix, *, mu_mature: float, reversion: ValuationDriftScaleReversion
) -> FloatMatrix:
    """Monthly log-drift as a function of realized log enterprise value `s = log V`.

        mu(s) = mu_mature + (mu_young - mu_mature) * exp(-max(0, s - s_onset) / s_scale)

    Vectorized over the rollout axis; `log_value` is the per-rollout `s` at a single timestep.
    Below the onset the company gets the full hot `mu_young`; above it, drift e-folds toward the
    mature asymptote as it grows.
    """

    excess = reversion.monthly_log_return_mu_young - mu_mature
    over_onset = np.maximum(0.0, log_value - reversion.log_value_onset_usd)
    return mu_mature + excess * np.exp(-over_onset / reversion.log_value_scale)


def _sample_company_valuation_vectorized(
    issuer: PrivateEquityRiskIssuerConfig, *, valuation_seeds: tuple[int, ...], horizon_months: int
) -> FloatMatrix:
    """Vectorized company-valuation V(t) sampler: returns (R, horizon_months + 1).

    Log-space Student-t random walk anchored at `current_valuation_usd`, driven by the
    valuation-specific RW params off an independent seed stream. Column 0 equals
    `current_valuation_usd` for every rollout. Only called when the valuation channel is
    enabled, so `current_valuation_usd` is not None.

    With constant drift this is a vectorized `cumsum` (same as the latent-mark RW). With
    scale-dependent reversion on, the drift at each step depends on the realized `log V` at that
    step, so V(t) is a genuine SDE and we integrate month by month (still vectorized across the
    rollout axis). The shocks are drawn identically in both branches, so turning reversion off
    is byte-identical to the constant-drift path.
    """

    # Caller only invokes this when the channel is on, so V0 is set; assert to narrow for mypy.
    current_valuation_usd = issuer.current_valuation_usd
    assert current_valuation_usd is not None
    rollout_count = len(valuation_seeds)
    paths = np.empty((rollout_count, horizon_months + 1), dtype=np.float64)
    paths[:, 0] = current_valuation_usd
    if horizon_months == 0:
        return paths
    rng = np.random.default_rng(_seed_from_rollout_seeds(valuation_seeds))
    shocks = rng.standard_t(df=issuer.valuation_student_t_nu, size=(rollout_count, horizon_months))
    shocks *= issuer.valuation_monthly_log_return_sigma

    log_v0 = math.log(current_valuation_usd)
    reversion = issuer.valuation_drift_scale_reversion
    if reversion is None:
        # Constant drift: closed-form cumulative sum (drift independent of the realized level).
        log_path = log_v0 + np.cumsum(issuer.valuation_monthly_log_return_mu + shocks, axis=1)
    else:
        # Scale-dependent drift: integrate the SDE step-by-step. Each step's drift is evaluated at
        # the realized log-value at the START of the step (explicit Euler), so the level reverts
        # as the company grows. Vectorized across rollouts; the month loop is cheap (H ~ 120).
        log_path = np.empty((rollout_count, horizon_months), dtype=np.float64)
        log_v = np.full(rollout_count, log_v0, dtype=np.float64)
        for m in range(horizon_months):
            drift = _scale_reverting_drift(log_v, mu_mature=issuer.valuation_monthly_log_return_mu, reversion=reversion)
            log_v = log_v + drift + shocks[:, m]
            log_path[:, m] = log_v

    try:
        with np.errstate(over="raise", invalid="raise"):
            paths[:, 1:] = np.exp(log_path)
    except FloatingPointError as error:
        raise ValueError("private-equity risk model produced non-finite company valuation") from error
    if not np.all(np.isfinite(paths)) or np.any(paths <= 0.0):
        raise ValueError("private-equity risk model produced invalid company valuation")
    return paths


def _scale_reverting_rate(
    log_value: FloatMatrix, *, rate_mature: float | FloatMatrix, reversion: ValuationDriftScaleReversion
) -> FloatMatrix:
    """Generic scale-reverting rate: `rate(s) = rate_mature + (rate_young - rate_mature) *
    exp(-max(0, s - s_onset) / s_scale)`, where `rate_young = reversion.monthly_log_return_mu_young`.

    The submodel field is named `monthly_log_return_mu_young` for V-drift semantics, but the
    shape is reused for hazard / mint rate, where the young value is reinterpreted as
    `lambda_young` / `mint_rate_young`. Doesn't enforce `rate_young >= rate_mature` here
    because the issuer-level validator only does so for the V-drift use; the mint-streams
    consumers documentation tells callers the young rate is the hot end.
    """

    excess = reversion.monthly_log_return_mu_young - rate_mature
    over_onset = np.maximum(0.0, log_value - reversion.log_value_onset_usd)
    return rate_mature + excess * np.exp(-over_onset / reversion.log_value_scale)


def _sample_company_valuation_and_shares_with_rounds_vectorized(
    issuer: PrivateEquityRiskIssuerConfig,
    *,
    valuation_seeds: tuple[int, ...],
    round_seeds: tuple[int, ...],
    mint_seeds: tuple[int, ...],
    horizon_months: int,
) -> tuple[FloatMatrix, FloatMatrix]:
    """Mint-streams V(t) and shares(t) sampler. Returns `(V, shares)` both `(R, H+1)`.

    V(t) integration mirrors `_sample_company_valuation_vectorized` (scale-reverting Student-t SDE
    between events) but with multiplicative jumps at primary-round event months. Shares evolve
    as a continuous employee-mint exponential plus the round-event share jumps.

    Three independent seed streams (`:pe_risk_valuation`, `:pe_risk_rounds`, `:pe_risk_mint`)
    so each generative quantity is uncorrelated with the others; mixing on the same seed
    stream would couple them artificially. Round events are sampled per-rollout via Bernoulli
    thinning at a possibly state-dependent hazard.

    `valuation[:, 0] = V0` and `shares[:, 0] = shares0` exactly; the latent_mark `current_mark *
    (V/V0) / (shares/shares0)` therefore equals `current_mark` at month 0 for every rollout.
    """

    primary = issuer.primary_round_config
    employee = issuer.employee_mint_config
    assert primary is not None
    assert employee is not None
    v0 = issuer.current_valuation_usd
    shares0 = issuer.shares_outstanding_initial
    assert v0 is not None
    assert shares0 is not None
    rollout_count = len(valuation_seeds)
    shape = (rollout_count, horizon_months + 1)
    valuation = np.empty(shape, dtype=np.float64)
    shares = np.empty(shape, dtype=np.float64)
    valuation[:, 0] = v0
    shares[:, 0] = shares0
    if horizon_months == 0:
        return valuation, shares

    valuation_rng = np.random.default_rng(_seed_from_rollout_seeds(valuation_seeds))
    round_rng = np.random.default_rng(_seed_from_rollout_seeds(round_seeds))
    mint_rng = np.random.default_rng(_seed_from_rollout_seeds(mint_seeds))

    v_shocks = valuation_rng.standard_t(df=issuer.valuation_student_t_nu, size=(rollout_count, horizon_months))
    v_shocks *= issuer.valuation_monthly_log_return_sigma

    # Per-rollout employee mint rate `m_r`, LogNormal-around-mature. `dS/dt = m_r * S` between
    # events, so per-month multiplicative factor is `(1 + m_r) ** (1/12)`. The optional
    # scale_reversion is applied per timestep using the realized log V.
    mint_z = mint_rng.standard_normal(rollout_count)
    mint_rate_mature_per_rollout = employee.annual_mint_rate_mature * np.exp(
        employee.annual_mint_rate_log_sigma * mint_z
    )

    # Pre-draw all per-month thinning uniforms and per-event cash/step-up draws. Each rollout
    # gets at most `horizon_months` rounds (events fire at integer months; ≥2 per month is
    # not modeled). Pre-drawing makes the integration loop pure numpy.
    hazard_uniforms = round_rng.uniform(size=(rollout_count, horizon_months))
    cash_over_v_pre_log = round_rng.normal(
        loc=math.log(primary.cash_over_v_pre_median),
        scale=primary.cash_over_v_pre_log_sigma,
        size=(rollout_count, horizon_months),
    )
    step_up_log = round_rng.normal(
        loc=math.log(primary.step_up_median), scale=primary.step_up_log_sigma, size=(rollout_count, horizon_months)
    )
    cash_over_v_pre_draws = np.exp(cash_over_v_pre_log)
    step_up_draws = np.exp(step_up_log)

    # IPO-anticipation decay reads the marginal CDF (same `public_market_cdf_anchors` /
    # `annual_public_market_probability` the regime sampler uses) and multiplies the hazard
    # by (1 - CDF(t)). Precompute the marginal CDF; doesn't depend on the rollout.
    if primary.ipo_anticipation_decay:
        ipo_cdf = _public_market_marginal_cdf(issuer, horizon_months=horizon_months)
    else:
        ipo_cdf = np.zeros(horizon_months + 1, dtype=np.float64)

    log_v = np.full(rollout_count, math.log(v0), dtype=np.float64)
    log_shares = np.full(rollout_count, math.log(shares0), dtype=np.float64)

    v_reversion = issuer.valuation_drift_scale_reversion
    hazard_reversion = primary.monthly_hazard_scale_reversion
    mint_reversion = employee.scale_reversion

    for m in range(horizon_months):
        # 1) V random walk between events (explicit Euler with scale-reverting drift if set).
        drift_v: FloatMatrix | float
        if v_reversion is None:
            drift_v = issuer.valuation_monthly_log_return_mu
        else:
            drift_v = _scale_reverting_drift(
                log_v, mu_mature=issuer.valuation_monthly_log_return_mu, reversion=v_reversion
            )
        log_v = log_v + drift_v + v_shocks[:, m]

        # 2) Employee mint: smooth monthly compounding at per-rollout rate `m_r`. With optional
        # scale-reversion, the effective per-rollout rate is `mint_mature_per_rollout`
        # interpolated by the same log-V shape.
        if mint_reversion is None:
            mint_per_rollout = mint_rate_mature_per_rollout
        else:
            mint_per_rollout = _scale_reverting_rate(
                log_v, rate_mature=mint_rate_mature_per_rollout, reversion=mint_reversion
            )
        # Smooth exponential: per-month factor = (1 + m_r) ** (1/12). Add to log_shares.
        np.add(log_shares, np.log1p(mint_per_rollout) / 12.0, out=log_shares)

        # 3) Primary-round event: Bernoulli thinning at state-dependent hazard.
        if hazard_reversion is None:
            hazard = np.full(rollout_count, primary.monthly_hazard, dtype=np.float64)
        else:
            hazard = _scale_reverting_rate(log_v, rate_mature=primary.monthly_hazard, reversion=hazard_reversion)
        if primary.ipo_anticipation_decay:
            hazard = hazard * (1.0 - ipo_cdf[m + 1])
        # Clip to [0, 1] in case scale-reversion overshoots.
        hazard = np.clip(hazard, 0.0, 1.0)
        fires = hazard_uniforms[:, m] < hazard

        if np.any(fires):
            # 4) Apply round jumps. V_post = V_pre * (1 + cash/V_pre) * step_up.
            #    shares_post = shares_pre * (1 + (cash/V_pre)/step_up).
            cash_over = cash_over_v_pre_draws[:, m]
            step_up = step_up_draws[:, m]
            log_v[fires] = log_v[fires] + np.log1p(cash_over[fires]) + np.log(step_up[fires])
            log_shares[fires] = log_shares[fires] + np.log1p(cash_over[fires] / step_up[fires])

        valuation[:, m + 1] = np.exp(log_v)
        shares[:, m + 1] = np.exp(log_shares)

    if not np.all(np.isfinite(valuation)) or np.any(valuation <= 0.0):
        raise ValueError("mint-streams sampler produced invalid V(t)")
    if not np.all(np.isfinite(shares)) or np.any(shares <= 0.0):
        raise ValueError("mint-streams sampler produced invalid shares(t)")
    return valuation, shares


def _public_market_marginal_cdf(issuer: PrivateEquityRiskIssuerConfig, *, horizon_months: int) -> FloatMatrix:
    """Marginal `P(public_market_opened_by_month_m)` over `0..horizon_months`.

    Uses the same anchor + flat-tail hazard model as the regime sampler. Returns a 1-D array
    of shape `(horizon_months + 1,)` with `cdf[0] = 0`. Used by the mint-streams sampler's
    optional IPO-anticipation decay; the regime sampler still draws its own per-rollout IPO
    times off a different seed stream.
    """

    months = np.arange(horizon_months + 1, dtype=np.float64)
    if not issuer.public_market_cdf_anchors:
        # Flat constant-per-year hazard over the whole horizon.
        if issuer.annual_public_market_probability <= 0.0:
            return np.zeros_like(months)
        monthly_hazard = 1.0 - (1.0 - issuer.annual_public_market_probability) ** (1.0 / 12.0)
        flat_tail: FloatMatrix = 1.0 - (1.0 - monthly_hazard) ** months
        return flat_tail

    # Piecewise-constant monthly hazard between anchors that reproduces the CDF exactly,
    # then a flat tail at `annual_public_market_probability` past the last anchor.
    cdf = np.zeros_like(months)
    prev_month, prev_cum = 0, 0.0
    for anchor in issuer.public_market_cdf_anchors:
        span = anchor.month - prev_month
        if span <= 0:
            prev_month, prev_cum = anchor.month, anchor.cumulative_probability
            continue
        survival_ratio = (1.0 - anchor.cumulative_probability) / max(1.0 - prev_cum, 1e-12)
        # Per-month survival factor that interpolates the anchor CDF exactly.
        per_month_survival = survival_ratio ** (1.0 / span)
        for t in range(prev_month + 1, anchor.month + 1):
            cdf[t] = 1.0 - (1.0 - prev_cum) * per_month_survival ** (t - prev_month)
        prev_month, prev_cum = anchor.month, anchor.cumulative_probability
    # Flat tail past the last anchor.
    if prev_month < horizon_months and issuer.annual_public_market_probability > 0.0:
        tail_monthly_hazard = 1.0 - (1.0 - issuer.annual_public_market_probability) ** (1.0 / 12.0)
        for t in range(prev_month + 1, horizon_months + 1):
            cdf[t] = 1.0 - (1.0 - prev_cum) * (1.0 - tail_monthly_hazard) ** (t - prev_month)
    elif prev_month < horizon_months:
        cdf[prev_month + 1 :] = prev_cum
    return cdf


def _dilution_factor(
    *,
    annual_dilution_rate: float,
    annual_dilution_rate_log_sigma: float,
    rollout_seeds: tuple[int, ...],
    issuer_id: str,
    rollout_count: int,
    horizon_months: int,
) -> FloatMatrix:
    """Per-rollout per-month dilution factor `(1 + r_i) ** (t / 12)`, shape `(R, T+1)`.

    M2.2-A: each rollout `i` draws its OWN rate

        r_i = annual_dilution_rate * exp(annual_dilution_rate_log_sigma * z_i),  z_i ~ N(0, 1)

    i.e. `r ~ LogNormal(mu=log(annual_dilution_rate), sigma=annual_dilution_rate_log_sigma)`,
    **median-anchored** at `annual_dilution_rate` (a LogNormal's median is `exp(mu)`). We
    deliberately do NOT mean-anchor: a mean-anchored draw would shift the typical realized
    dilution UP by `exp(sigma**2 / 2)` as sigma grows, biasing the central mark path.

    ONE unified path -- there is NO `if sigma == 0` / `if rate == 0` branch:

    * The `z` draw comes off its OWN derived seed stream (`<issuer>:pe_risk_dilution`), mixed
      exactly like `_sample_company_valuation_vectorized`. Because it is independent of the
      level/event/valuation streams, drawing it cannot perturb the mark/event/regime/valuation
      arrays -- only the per-share mark *scale* moves when sigma turns on.
    * With `annual_dilution_rate_log_sigma == 0` (the default) every `z_i` collapses via
      `exp(0.0 * z_i) == exp(0.0) == 1.0` exactly, so `r_i == annual_dilution_rate` for every
      rollout (bit-for-bit) and each row equals the M2 deterministic `(1 + rate) ** (t / 12)`.
      sigma=0 thus degenerates *naturally*, byte-identical to M2, with no special-case.

    `dilution_factor(0) == 1`. The result broadcasts elementwise against the `(R, T+1)`
    valuation ratio in the coupled-mark formula. The discrete primary-round event kind
    (M2.2-C) and the full Bayesian posterior (M2.2-D) remain deferred.
    """

    months = np.arange(horizon_months + 1, dtype=np.float64)
    # Independent per-rollout draw. Its OWN seed stream => it cannot perturb the
    # level/event/valuation RNG streams (mark/event/regime/valuation stay byte-identical
    # when sigma flips on; only the per-share mark scale changes).
    dilution_seeds = derive_stream_rollout_seeds(rollout_seeds, stream_id=f"{issuer_id}:pe_risk_dilution")
    rng = np.random.default_rng(_seed_from_rollout_seeds(dilution_seeds))
    z = rng.standard_normal(rollout_count)
    r = annual_dilution_rate * np.exp(annual_dilution_rate_log_sigma * z)
    return np.power(1.0 + r[:, None], months[None, :] / 12.0)


def _sample_event_month_mask_vectorized(
    *, median_months: float, log_sigma: float, rng: np.random.Generator, rollout_count: int, horizon_months: int
) -> BoolMatrix:
    """Sample LogNormal(median, sigma) inter-arrival event months for each of R rollouts and
    return a (R, horizon_months + 1) boolean mask of when events fire.
    """

    mask = np.zeros((rollout_count, horizon_months + 1), dtype=np.bool_)
    if median_months <= 0.0 or rollout_count == 0 or horizon_months == 0:
        return mask
    # Conservative upper bound on the number of events per rollout: enough that the
    # cumulative time exceeds horizon with overwhelming probability.
    max_events = max(8, math.ceil(horizon_months / max(median_months, 1.0)) * 4)
    intervals = rng.lognormal(mean=math.log(median_months), sigma=log_sigma, size=(rollout_count, max_events))
    intervals = np.maximum(intervals, 1.0)
    cumulative = np.cumsum(intervals, axis=1)
    months = np.rint(cumulative).astype(np.int64)
    in_window = (months >= 1) & (months <= horizon_months)
    rows = np.repeat(np.arange(rollout_count), max_events)
    flat_months = months.reshape(-1)
    flat_in_window = in_window.reshape(-1)
    mask[rows[flat_in_window], flat_months[flat_in_window]] = True
    return mask


def _noisy_mark_vectorized(
    values: FloatMatrix, *, rng: np.random.Generator, log_noise_mu: float, log_noise_sigma: float
) -> FloatMatrix:
    if log_noise_mu == 0.0 and log_noise_sigma == 0.0:
        return values
    return values * np.exp(rng.normal(loc=log_noise_mu, scale=log_noise_sigma, size=values.shape))


def _seed_from_rollout_seeds(seeds: tuple[int, ...]) -> int:
    """Deterministic uint64 seed mixed from a sequence of arbitrary-precision ints."""

    digest = hashlib.sha256()
    for seed in seeds:
        digest.update(int(seed).to_bytes(32, "big", signed=False))
    return int.from_bytes(digest.digest()[:8], "big")


def _monthly_probability(annual_probability: float) -> float:
    if annual_probability <= 0.0:
        return 0.0
    if annual_probability >= 1.0:
        return 1.0
    return float(1.0 - math.pow(1.0 - annual_probability, 1.0 / 12.0))


def _public_market_open_hazard_by_month(
    issuer: PrivateEquityRiskIssuerConfig, horizon_months: int
) -> npt.NDArray[np.float64]:
    """Per-month going-public hazard vector (length horizon_months+1), indexable by month t.

    Index 0 is unused (the loop starts at month 1). Without anchors the whole vector is
    the flat per-month rate from `annual_public_market_probability`. With anchors, each
    bucket (m_i, F_i) -> (m_{i+1}, F_{i+1}) gets the constant monthly hazard that exactly
    reproduces the survival drop S_{i+1}/S_i over its m_{i+1}-m_i months; the first bucket
    runs from month 0 (S=1) to the first anchor. Months past the last anchor fall back to
    the flat `annual_public_market_probability` tail hazard.
    """

    tail_hazard = _monthly_probability(issuer.annual_public_market_probability)
    hazard = np.full(horizon_months + 1, tail_hazard, dtype=np.float64)

    prev_month = 0
    prev_survival = 1.0
    for anchor in issuer.public_market_cdf_anchors:
        anchor_month = min(anchor.month, horizon_months)
        survival = 1.0 - anchor.cumulative_probability
        bucket_hazard = 1.0 - (survival / prev_survival) ** (1.0 / (anchor.month - prev_month))
        hazard[prev_month + 1 : anchor_month + 1] = bucket_hazard
        prev_month = anchor.month
        prev_survival = survival
        if prev_month >= horizon_months:
            break

    return hazard
