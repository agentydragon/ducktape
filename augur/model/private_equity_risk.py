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
        return self


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
                    rollout_count=rollout_count,
                    horizon_months=horizon_months,
                )
            )

        sampled = SampledExogenousBundle(
            levels=SERIES_LEVELS_SCHEMA.to_frame(),
            private_equity=PrivateEquityBundle.combine(pe_bundle_parts),
            metadata={
                "exogenous_model_id": self.label,
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

    latent_mark = _sample_latent_marks_vectorized(issuer, level_seeds=level_seeds, horizon_months=horizon_months)

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
