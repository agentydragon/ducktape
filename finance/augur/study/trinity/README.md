# augur/study/trinity

Cooley, Hubbard and Walz, "Retirement Savings: Choosing a Withdrawal Rate That Is
Sustainable", _AAII Journal_ XX(2), February 1998 — the paper the "4% rule" comes from. Its
Table 3 (inflation-adjusted withdrawals, 1926 to 1995) is the target.

`replay.py` builds the study's scenario, replays every 30-year window the period supplies, and
reports success rates and SAFEMAX per allocation. Its module docstring carries the
deviations and numerical attribution from the earlier configured-runner investigation;
those historical measurements are not a fresh sourced validation of the Python migration.
`evidence_snapshot.py` fetches the record the replay runs on.

The experiment owns a Python monthly batch loop over `ActionSession`. `policy.py`
proposes overweight-first/FIFO sales using `policy.sleeves.withdraw`, then explicit
payments. The scenario contains no configured allocator. Fixed indexed withdrawals
remain scheduled claims at months 0, 12, …, 348: each scales the original annual
amount by current/origin CPI, never the previous rounded withdrawal. Distributions
arrive before the decision, including the bond model's opening coupon. Surplus
coupons stay cash; there are no purchases or drift rebalancing. Snapshot 360 marks
the final books without another payout. Tax-free accounting and the existing bond
proxy are unchanged; this is not exact paper reproduction.

Success means completing this closed withdrawal schedule without a stop, including
exact exhaustion after the last paid withdrawal. A rejected payment retains prior
sales and stops that window; there is no retry. The other windows continue.

`Replay.window_starts` maps each output rollout to its historical start date.
The study selects every eligible month; smaller date-selected batches use
`HistoricalWindowsModel.materialize(window_starts=..., horizon_months=...)` and
preserve each selected path. `Replay.run(rollout_ids=[2, 0], capture="forensic", ...)`
instead selects original IDs from the already materialized population. This interface
does not use random seeds. Summary capture retains canonical observed outcomes and
stopped books, not complete journals; selected forensic replay uses the same policy.

Offline path/trace identity checks run in CI:

```bash
bbr test //finance/augur/study/trinity:test_path_identity
bbr test //finance/augur/study/trinity:test_actions
```

An offline CLI run uses generated placeholder history through the same historical
window and bond-product construction. These inputs test plumbing, not forecasts or
published-table agreement:

```bash
bbr run //finance/augur/study/trinity:replay_bin -- \
  --synthetic --equity-share 0.5 --withdrawal-rate 0.04 \
  --output-dir /tmp/trinity-example --trace-rollout 2 --trace-rollout 0
```

`study.json` records the inputs and window-start mapping; `outcomes.json` contains
the population summaries and `traces.json` the selected detailed rollouts. Use
`--evidence-dir PATH` instead of `--synthetic` for an existing evidence checkout.
The all-stock, mixed and all-bond CLI cells, selected replay, original-base CPI,
coupon cash retention, final payout boundary and exact-depletion controls run in CI.

The full reproduction targets are `manual` (see the parent README) — network-dependent and minutes long:

```bash
bbr test //finance/augur/study/trinity:replay_test
bbr run //finance/augur/study/trinity:replay_bin   # the full table, printed
```
