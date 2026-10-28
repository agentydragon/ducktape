# Trinity replay timeout, 2026-10-27

The manual `//finance/augur/study/trinity:replay_test` run at commit
`7ac6630f37729ccfd453b1738d80b10439a22f53` failed in BuildBuddy invocation
`1827e6f5-86a6-44d6-bb2d-abc03db81095`: four tests passed, then three timed out.
The JUnit case durations were 2.259, 0.008, 71.610, 589.426, 145.192, 1.000,
and 1.000 seconds in collection order. The final three stacks were executing
`sim.agent.assemble`, `sim.schedule.is_due`, and `sim.holdings._selected`.

`replay_test.py` runs a 474-window, 360-month historical simulation for each
allocation and withdrawal-rate cell. `Replay.success_rate` calls `run`, which
composes and drives all windows through `ActionSession`; `safemax` bisects its
grid with further full-cell runs. The root `conftest.py` gives each unmarked test
the remainder of 90% of Bazel's *target* timeout, measured from module import.
With the `large` target's 900-second budget, the table test had only 145 seconds
left after SAFEMAX, and the later cases got the one-second minimum. These stacks
and elapsed times support active computation exhausting a shared budget, rather
than waiting on the evidence fetch or a stuck simulator step.

A bounded `cProfile` run through the same `Replay.run` action path with four
synthetic 360-month windows recorded 1,652,503 calls in 0.581 profiled seconds.
`ActionSession.advance` accounted for 0.529 cumulative seconds, with
`World.open_month` at 0.305 and `World.open_mortgages` at 0.211. This profile
is a call-path sample, not a historical-cell timing estimate: four generated
windows all succeeded, while historical windows can stop at different months.
The temporary profiling Bazel target was removed after the run; raw stats remain
at `/tmp/trinity-replay-temp.prof` on the diagnosing host.

Four Bazel shards distribute the seven existing cases by collection position:
shard 1 checks the record and table, shard 2 the input returns and all-bond row,
shard 3 the 4% claim and monotonicity, and shard 4 SAFEMAX. Each shard samples
the same named public evidence and checks all historical windows in its cells.
The manual suite remains network dependent; the four shards fetch separately.
