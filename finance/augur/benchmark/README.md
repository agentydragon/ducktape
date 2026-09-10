# Augur benchmark

The feature-rich scenario the engine is measured on.

`scenario.py` authors it as a `Scenario` and its sampled paths rather than as the engine's own
input format, so what is measured is a compiled plan and not a hand-written fixture — the same
direction [../sim/testing/case.py](../sim/testing/case.py) takes, and for the same reason.
Independent agents are combined deliberately: the scenario exercises the supported policy
surface without one policy family starving another's liquidity.

The canonical shape is 60 monthly transitions plus the month-zero snapshot, a configurable
rollout count, 16 modeled cash accounts, scheduled and recurring transfers, deductions and
obligations, allocation and private-equity and TLH lots, four par-only bond/TIPS holdings, 17
row-major exact external series, and a 60-month property, mortgage, residency, rental,
improvement and sale lifecycle.

## Driver

`driver.py` runs the workload through the Python-controlled configured runner.
Choose the population size explicitly:

```sh
bbr run -c opt //finance/augur/benchmark:driver_bin -- \
  --output-mode compact --rollouts 100 --horizon-months 60 --repeats 5
```

`dense` retains monthly state and canonical events; `compact` retains terminal
summaries without allocating dense histories. Both include the full scenario,
including its Python TLH component, housing and private-equity lifecycle.
The CLI runs with one rollout in both modes in Bazel CI.

Scenario construction and compilation happen outside `timeit`'s measured regions.
Each cold/warm execution includes runtime initialization, Python orchestration,
native financial steps and output encoding. Output hashing happens afterward.
Memory is the process-wide high-water mark, including scenario preparation, not
an isolated native heap measurement. `timeit` disables cyclic garbage collection
while measuring. There is no speed threshold or large-N performance gate.

Historical native-only timings are not comparable to this scope: the caller loop,
TLH representation, capture and serialization work differ. No speedup or slowdown
is claimed by moving this driver.
