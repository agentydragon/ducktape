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
the remainder of 90% of Bazel's _target_ timeout, measured from module import.
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

The paper resolves SAFEMAX only to `[3%, 4%)`. The full table already runs those
two cells for each equity-holding allocation, so the manual test now asserts
100% survival at 3% and less than 100% at 4% from those results. The 31-point
historical bisection added no published-claim coverage. An offline flat-portfolio
case checks the bisection's known boundary, and four synthetic windows check
withdrawal monotonicity across stock, mixed, and bond allocations with a real
survival-to-exhaustion transition.

The manual suite now runs 33 full-population cells across two Bazel shards:
the 4% headline, full Table 3 neighbourhood, and all-bond direction still use
all 474 historical starts and the 360-month horizon. Both shards fetch the
named public evidence separately, so this remains an opt-in network test.
