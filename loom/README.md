# loom

Interpolates prediction-market **marginals** ("P(S&P ≥ X by date Y)", "P(OpenAI
IPO by 2028)") into **rollouts**: weighted sets of coherent world trajectories —
dense monthly numeric series plus discrete event streams that satisfy
structural validity — whose implied marginals land on the market prices.

Standalone program, deliberately loosely coupled to augur: loom emits a
serialized **WorldSet** artifact; augur (the first consumer) reads it through a
thin bridge on augur's side. `//loom/...` never depends on
`//finance/augur/...`.

## gym

`loom/gym/` is the forecasting eval: resolved tasks (question + info-cutoff
`as_of` + realized outcome), proper-loss scoring (log/Brier, pinball), a
registry of LLMs with asserted weight-freeze cutoffs (a model may only
forecast tasks whose `as_of` is at or after its cutoff), and the bare-prompt
LLM baseline every fancier method must beat:

```bash
ZAI_API_KEY=... bazelisk run //loom/gym:baseline_eval -- --model-id glm-4.5
```

Status: gym core landed; pipeline at plan stage — see <plans/program_plan.md>.
The position and prior experiments this program executes live in augur:
`finance/augur/plans/interpolating_prediction_markets.md`,
`finance/augur/plans/exogenous_rollout_architecture.md`, and the
`finance/augur/x/pm_reifier/` spike.
