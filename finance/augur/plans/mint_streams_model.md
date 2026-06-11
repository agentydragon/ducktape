# Augur — Mint-Streams Private-Equity Model

Replace the current smooth-dilution channel with a structurally honest decomposition:
discrete primary-round events + a continuous employee-mint stream + a between-events
V random walk. Land as a sibling preset (`bayesian_mint_streams`) that will become the
deployment default once it passes sanity. Original `bayesian` preset stays around for
A/B comparison until it's clearly worse.

## Motivation

The current `bayesian` preset has the M2.2-D scale-reverting V drift but a flat
per-rollout `annual_dilution_rate` (currently posterior median ~28%/yr fit on OpenAI
2019–2026 evidence). The fitter's priors are asymmetric — V drift is tightly anchored
toward an S&P-mature 10%/yr, dilution is wide and "let the data speak." On the in-sample
boom (where primary rounds co-moved V and shares) the likelihood routes the explosion
into dilution. Forward sim then runs V drift down toward the conservative mature
asymptote while dilution stays pinned at the boom-era 28%/yr — the central rollout
sees per-share mark fall from $687 → ~$280 in 10 years while V quintuples. That's a
modeling artifact, not a forecast.

The root cause is structural: the model has no concept of a discrete primary round.
Both V and shares are smooth processes, so they can't co-jump at the events that
_actually_ couple them in real-life private-co dynamics. Adding scale-reversion to
dilution would fix the symptom (the conservative-V-mature / boom-r-stuck-on imbalance)
but not the cause. The honest fix is to model the events.

The defect is already documented in `gaffer-private/.../evidence.md`:

> Summed primary-round dilution (~4-14% per round) undershoots the implied-share growth
> from valuation/price ratios (~30%/yr) — the gap is employee-equity (PPU/RSU) issuance
> plus the PBC recap — so a future dilution model needs a baseline continuous mint, not
> funding rounds alone.

## Mechanical decomposition

Three real-world drivers of share-count and valuation, each modeled separately:

| Stream               | Cadence                     | Effect on V                       | Effect on shares                       |
| -------------------- | --------------------------- | --------------------------------- | -------------------------------------- |
| **Primary round**    | Lumpy Poisson, ~1 / 12–24mo | Jumps up by `V_pre + cash_raised` | Jumps up by `cash_raised / price_post` |
| **Employee mint**    | Continuous flow             | None (SBC is non-cash)            | Smooth ~m/yr growth, scale-revertable  |
| **Secondary tender** | Sparse                      | None                              | None                                   |
| **V random walk**    | Between events              | Scale-reverting Student-t drift   | None                                   |

Tender events stay modeled as today (price observations with a discount + noise on
latent V/S), and don't affect V or share count.

## Schema additions

In `augur/model/private_equity_risk.py`, extend `PrivateEquityRiskIssuerConfig` with two
opt-in sub-configs. When `primary_round_config` is set, the new event-driven channel
activates and the existing smooth `annual_dilution_rate` is ignored. When unset, the
legacy v1/M2.2-D smooth-dilution channel runs verbatim (zero-regression for the current
`bayesian` preset).

```python
class PrimaryRoundConfig(FrozenModel):
    """Discrete primary-round event stream.

    Each round event simultaneously raises V by `cash_raised` and dilutes shares by an
    implied amount. Hazard, cash-size, and step-up are all stochastic per rollout.
    """
    # Hazard: per-month Poisson rate. Optional scale-reversion (decays as company matures,
    # mirroring V drift) and IPO-anticipation decay (rounds dry up close to IPO).
    monthly_hazard: float = Field(gt=0, le=1.0)
    monthly_hazard_scale_reversion: ValuationDriftScaleReversion | None = None
    """Optional: lambda(s) = lambda_mature + (lambda_young - lambda_mature) *
    exp(-max(0, s - onset)/scale), s = log V. Reuses the same shape submodel as V drift."""

    ipo_anticipation_decay: bool = False
    """If True, multiply hazard by (1 - P(public_market_opened_by_t)) so rounds taper as
    IPO approaches. Reads from the same `public_market_cdf_anchors` as the existing model."""

    # Round size: cash raised as fraction of V_pre. LogNormal(median, log_sigma).
    cash_over_v_pre_median: float = Field(gt=0)
    cash_over_v_pre_log_sigma: float = Field(default=0.5, ge=0)

    # Step-up: V_post / (V_pre * (1 + cash/V_pre)) accounts for the info-driven repricing
    # at a round (rounds typically come at higher valuations than the smooth random walk
    # would predict). Default 1.0 = pure mechanical V_post = V_pre + cash.
    step_up_median: float = Field(default=1.0, gt=0)
    step_up_log_sigma: float = Field(default=0.0, ge=0)


class EmployeeMintConfig(FrozenModel):
    """Continuous employee equity issuance.

    `dS_emp/dt = m(t) * S(t)`, smooth exponential. No effect on V (SBC is non-cash).
    """
    annual_mint_rate_mature: float = Field(default=0.03, ge=0)  # ~3%/yr mature large-cap
    annual_mint_rate_log_sigma: float = Field(default=0.0, ge=0)  # per-rollout dispersion
    scale_reversion: ValuationDriftScaleReversion | None = None
    """Optional: mint rate decays from young (e.g. ~8%/yr) toward mature (e.g. ~3%/yr) as
    log V grows past onset. Empirically late-stage tech mint is fairly stable across
    maturity, so this is usually unnecessary; default off."""
```

On `PrivateEquityRiskIssuerConfig`, add:

```python
primary_round_config: PrimaryRoundConfig | None = None
employee_mint_config: EmployeeMintConfig | None = None

# Validators:
# - primary_round_config and employee_mint_config must be set together or both unset.
# - When set, current_valuation_usd + shares_outstanding_initial must also be set
#   (mint streams require an honest cap-table anchor).
# - When set, the legacy annual_dilution_rate / annual_dilution_rate_log_sigma fields
#   must be zero (cannot mix smooth + event dilution channels).
```

## Sampler design

Replace the current `dilution_factor(t)` smooth path with a per-rollout event-driven
trajectory in `_sample_private_equity_paths_vectorized`:

```
For each rollout r:
  1. Sample primary-round event times via thinned Poisson on monthly_hazard(s,t).
     (Scale-dependent + optional IPO-anticipation decay → use thinning rather than
     direct inversion.)
  2. Sample V(t) on the monthly grid:
     - Between events: integrate scale-reverting Student-t SDE (same as current code).
     - At event time T_k:
        - Draw cash_over_V_pre_k from LogNormal(median, log_sigma).
        - Draw step_up_k from LogNormal(step_up_median, step_up_log_sigma).
        - V(T_k+) = V(T_k-) * (1 + cash_over_V_pre_k) * step_up_k.
  3. Sample shares(t):
     - Per-rollout employee mint rate m_r from LogNormal(annual_mint_rate_mature, log_sigma)
       (or scale-revertable equivalent).
     - Between events: dS/dt = m_r * S, smooth exponential.
     - At event T_k: shares(T_k+) = shares(T_k-) * (1 + cash_over_V_pre_k / step_up_k).
       (Algebra: price_pre = V_pre/S_pre; price_post = price_pre * step_up_k;
        new_shares = cash / price_post = S_pre * (cash/V_pre) / step_up_k.)
  4. mark(t) = V(t) / shares(t). Tender / admin / IPO event observation noise rides
     on top exactly as today (no change to the price-observation channel).
```

Vectorization: V(t), shares(t), mark(t) are all `(R, T+1)` matrices like today. The
event times are per-rollout sparse — use a dense `event_mask: BoolMatrix (R, T+1)` and
a `cash_over_v_pre_at_event: FloatMatrix (R, T+1)` so the SDE integration scans linearly
over months.

## Sibling preset

Add to `gaffer-private/k8s/augur/config.yaml`:

```yaml
bayesian_mint_streams:
  type: composite
  macro: *macro_state_space
  private_equity:
    type: private_equity_risk
    issuers:
      openai:
        # ... shared fields (current_mark_usd, current_valuation_usd, ...) ...
        # New event-driven channel:
        primary_round_config:
          monthly_hazard: <fitted>
          cash_over_v_pre_median: <fitted>
          cash_over_v_pre_log_sigma: <fitted>
          step_up_median: <fitted>
          step_up_log_sigma: <fitted>
          monthly_hazard_scale_reversion: ...
          ipo_anticipation_decay: true
        employee_mint_config:
          annual_mint_rate_mature: <fitted>
          annual_mint_rate_log_sigma: <fitted>
        # Legacy smooth-dilution params zeroed:
        annual_dilution_rate: 0.0
        annual_dilution_rate_log_sigma: 0.0
        # V drift + IPO CDF + tender / forced-sale / collapse params unchanged from `bayesian`.
```

The `bayesian` preset stays unchanged for A/B comparison. Once `bayesian_mint_streams`
passes sanity bands and the calibration KL is competitive, flip `default_model_id`
to point at it.

**Drop the soft-cap.** The `TrainedPrivateEquityScalePrior` boom-tamer (a drift penalty
on implausibly large `V`) becomes redundant once scale-reverting drift + event-driven
primary rounds anchor V mechanically. Remove from the new preset; revisit only if the
forward tails blow out.

## Phase B — Bayesian fitter

New module `augur/fit/bayes_mint_streams.py` (or extend `bayes_dilution.py`). Joint
posterior over:

| Parameter                                                      | Prior                                           | Identifiable?                                                                                                                                                                            |
| -------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `monthly_hazard` (or hazard rate at the issuer's current size) | LogNormal(median=1/18 per month, log_sigma=0.5) | Yes — directly observed via round event count over the observation window.                                                                                                               |
| `cash_over_v_pre_median`                                       | LogNormal(median=0.08, log_sigma=0.6)           | Yes — observed per-round cash/V_pre ratios.                                                                                                                                              |
| `cash_over_v_pre_log_sigma`                                    | HalfNormal(0.3)                                 | Yes — across-round dispersion.                                                                                                                                                           |
| `step_up_median`                                               | LogNormal(median=1.0, log_sigma=0.2)            | Partially — the V_post / (V_pre + cash) excess factor is identifiable from the difference between observed V_post and the mechanical V_pre + cash. With limited rounds, prior dominates. |
| `step_up_log_sigma`                                            | HalfNormal(0.2)                                 | Partial.                                                                                                                                                                                 |
| `annual_mint_rate_mature`                                      | LogNormal(median=0.03, log_sigma=0.5)           | Identifiable from the residual (implied share growth not attributable to primary rounds).                                                                                                |
| V-drift params                                                 | Same as current M2.2-D priors                   | Same.                                                                                                                                                                                    |
| V-vol (`sigma_v`)                                              | Same as current M2.2-D                          | Should sharpen further now that primary jumps don't have to be fit by the random walk.                                                                                                   |

The numpyro model treats each `valuation_kind == "primary"` observation as a discrete
event likelihood: observed V_post and observed `cash_raised_usd` constrain both V(t) at
the event time and the implied dilution. `valuation_kind == "secondary"` observations
constrain V(t) without a share-count effect. `valuation_kind == "implied"` (no real
event behind it — used only for synthetic test data) is treated as a noisy V(t)
observation.

Identifiability check: 6 primary rounds + 3 secondary tenders + ~10 tender prices over
2019–2026 should be enough to pin all but `step_up_*` and the scale-reversion shape
(latter stays fixed at prior centers, same as M2.2-D today).

## Phase C — Wire-up

1. Refit on the new annotated `observations.jsonl`. Write posterior summary to
   a new artifact (e.g. `openai_mint_streams_artifact.json`).
2. Add the `bayesian_mint_streams` preset to `gaffer-private/k8s/augur/config.yaml`,
   parameters from the artifact.
3. Run `//gaffer_augur/openai_stock/models:sample_sanity_test` against the new preset.
   Update `sample_sanity.yaml` `[TARGET]` bands if the central mark trajectory has
   changed shape (we expect it to: per-share roughly flat-to-up, V continuing to grow).
4. Eyeball the calibration tab in the live frontend with `?x=bayesian_mint_streams`.
5. Once sanity passes and visual goldens are stable, flip
   `default_model_id: bayesian_mint_streams`. Keep `bayesian` available for
   regression comparison.

## Deferred / out-of-scope for v1

- **Macro-conditional round hazard.** Rounds dry up in liquidity-suspended /
  distressed macro states. Hook exists on the macro side; v1 ignores it.
- **Round-vs-tender coupling.** Large primary rounds often trigger employee tenders
  (Oct 2024 primary + Nov 2024 tender). v1 keeps them independent.
- **Cap-table fidelity (preferred vs common, liquidation preferences).** Class A common
  holders may not realize the full enterprise value at exit. Still modeled separately
  via the forced-sale / acquisition-haircut paths.
- **2026-02 round complexity (contingent + services).** The annotated `cash_raised_usd`
  is the announced cash headline; non-cash compute commitments + AGI-contingent tranches
  are not separately weighted. v2 could split into unconditional/contingent/services
  with different V-jump factors.
- **MSFT 2019 / 2023-01 profit-share legs.** Annotated as primary at $X post; the actual
  cap stack is more complex.

## Open decisions before Phase A code

1. Should `step_up_log_sigma > 0` even when the prior is weak, or is `step_up_median=1.0`
   - `log_sigma=0` (pure mechanical) a defensible v1?
2. Does `employee_mint_config` need its own scale-reversion, or is a flat per-rollout
   mint rate honest enough for v1? (Late-stage tech is fairly stable.)
3. The Oct 2025 tender at $500B is a _secondary_ but the V observation is real. Should
   `valuation_kind == "secondary"` observations constrain V(t) directly, or only weakly
   (since tender prices are info-incomplete)? Likely answer: constrain with a wider
   `uncertainty_log_sigma` than primary rounds, which the annotations already do (0.10
   for secondary vs 0.05 for primary).
