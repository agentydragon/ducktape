# PM-reifier spike: is an LLM a viable _base measure_?

Throwaway experiment for `augur/plans/interpolating_prediction_markets.md`. **Not production
code** — it lives in `x/` and is not Bazel-built.

## The question

The plan reifies prediction-market _marginals_ into a sampleable _joint_ over trajectories by
min-KL projection from a **base measure `Q`**. `Q` can be our structured models or an LLM. This
spike asks: **can an LLM be `Q`** — emit a diverse cloud of typed numeric scenarios that, after
**one max-ent reweight to the market prices**, match those prices _without the effective sample
size collapsing_? (Collapsing ESS = the LLM never proposed worlds in the market-implied region, so
reweighting is fiction.)

## What it does (`run_spike.py`)

- prompts GLM-4.6 (z.ai coding endpoint, `thinking` disabled) for `8 × 10 = 80` scenarios, each a
  JSON object with year-end knots (2026–2032) for `sp500`, `btc`, OpenAI valuation, plus an OpenAI
  `ipo_month_index`. A deterministic interpolator would fill monthly; these markets only need the
  knots, so none is needed here.
- two variants: **unconditioned** (the LLM's own world-prior — the real base-measure test) and
  **conditioned** (the market prices are shown — an upper bound on calibration).
- computes each market's model probability = fraction of scenarios satisfying it, then **reweights**
  the empirical sample (`w_i ∝ exp(Σ λ_m·indicator)`, ridge-softened so incoherent targets degrade
  gracefully) so the reweighted marginals match the crowd prices; reports **ESS = 1/Σwᵢ²**.
- logs every request+response to `transcripts/`, scenarios + summary to `results/`, and the z.ai
  weekly-quota delta.

Run: `python3 augur/x/pm_reifier/run_spike.py` (key from `$ZAI_API_KEY` or `/tmp/zai_key`, mirrored
from the `claude-sandbox` `zai-api-key` secret). Market prices here are **illustrative** plausible
values (a coherent monotone ladder), not pulled live.

## Findings (2026-06-04, GLM-4.6, 160 scenarios)

**Feasibility: yes.** The LLM is a viable base measure.

- **100% valid** typed-numeric JSON (80/80 each variant) — structured output is reliable.
- **ESS does not collapse**: unconditioned **57%**, conditioned **76%** of the sample after
  reweighting — the cloud genuinely covers the market-implied regions.
- after reweighting, all 8 markets land on the crowd prices (units-corrected):

  | market              | price | raw (uncond.) | reweighted |
  | ------------------- | ----: | ------------: | ---------: |
  | sp500>6000@2027     |  0.55 |          0.29 |       0.47 |
  | sp500>8000@2030     |  0.45 |          0.24 |       0.41 |
  | sp500>10000@2032    |  0.38 |          0.20 |       0.38 |
  | btc>150k@2027       |  0.50 |          0.23 |       0.42 |
  | btc>500k@2030       |  0.30 |          0.09 |       0.25 |
  | openai_ipo<=2027    |  0.30 |          0.41 |       0.43 |
  | openai_ipo<=2029    |  0.65 |          0.64 |       0.65 |
  | openai_val>1T@2030  |  0.55 |          0.60 |       0.60 |

- **conditioning helps**: shown the prices, the LLM self-calibrates — raw marginals move much closer
  to target (e.g. sp500>6000 0.29 → 0.42), so less tilt is needed and ESS rises (57% → 76%).

**Cost: negligible.** 160 scenarios = **50,147 tokens**, and the z.ai **weekly** quota moved
`2% → 2%` (**0 pp**). `thinking: disabled` is essential (it zeroed ~500 reasoning tokens/call seen
with thinking on). A production-scale run (thousands of scenarios over the full catalog) is trivially
affordable.

### Two real caveats the spike surfaced

1. **Numeric units discipline.** GLM emitted OpenAI valuations in **trillions** despite an "in USD"
   schema (values like `12.5` = $12.5T) — initially read as `0.00` against a `1e12` threshold. The
   numbers themselves are sane; the model just chose natural units. A production reifier must pin
   units per field (or accept a units field) and range-validate before it feeds `sim`.
2. **Conservative-upside bias.** The LLM's _raw_ marginals sit systematically **below** the crowd on
   equity/crypto upside (it under-weights boom tails even when told to include them). Reweighting
   corrects the marginals, but a biased base costs ESS; conditioning on the prices mitigates it.

### Read

So an LLM looks like a usable base measure for the macro/level/IPO markets: rich coupling, reliable
typed output, cheap, and it covers the right regions. The hybrid in the plan — **LLM proposes the
coupling, a reweight layer enforces the marginals** — is supported by this run. Next would be: real
catalog prices, a bigger `K`, monthly interpolation for date-specific markets, and a
structured-`Q` baseline to compare ESS/coverage against.
