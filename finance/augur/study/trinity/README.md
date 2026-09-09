# augur/study/trinity

Cooley, Hubbard and Walz, "Retirement Savings: Choosing a Withdrawal Rate That Is
Sustainable", _AAII Journal_ XX(2), February 1998 — the paper the "4% rule" comes from. Its
Table 3 (inflation-adjusted withdrawals, 1926 to 1995) is the target.

`replay.py` builds the study's scenario, replays every 30-year window the period supplies, and
reports success rates and SAFEMAX per allocation. Its module docstring carries the
attribution: every way this differs from the paper, and how close the result lands.
`evidence_snapshot.py` fetches the record the replay runs on.

`Replay.window_starts` maps each output rollout to its historical start date.
The study selects every eligible month; smaller date-selected batches use
`HistoricalWindowsModel.materialize(window_starts=..., horizon_months=...)` and
preserve each selected path. This interface does not use random seeds.

Offline path/trace identity checks run in CI:

```bash
bbr test //finance/augur/study/trinity:test_path_identity
```

The full reproduction targets are `manual` (see the parent README) — network-dependent and minutes long:

```bash
bbr test //finance/augur/study/trinity:replay_test
bbr run //finance/augur/study/trinity:replay_bin   # the full table, printed
```
