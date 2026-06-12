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
forecast tasks whose `as_of` is at or after its cutoff), and an Inspect `react`
agent contestant in a date-clamped Docker sandbox. The agent's only network
route is the wayback proxy sidecar, which serves the pre-cutoff archived web;
`--no-archive` drops the proxy entirely so the agent forecasts from the mounted
`/data` files and its own knowledge alone — the floor every archive-enabled or
fancier method must beat:

```bash
LITELLM_API_KEY=... bazelisk run //loom/gym:agent_eval_bin -- \
    --model-id glm-4.5 --log-dir /tmp/gym-logs --no-archive
```

Status: gym core landed; pipeline at plan stage — see <PLAN.md>.
The position and prior experiments this program executes live in augur:
`finance/augur/plans/interpolating_prediction_markets.md`,
`finance/augur/plans/exogenous_rollout_architecture.md`, and the
`finance/augur/x/pm_reifier/` spike.
