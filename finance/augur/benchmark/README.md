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

The driver that runs it lives beside the engine, in
[../rust/benchmark/README.md](../rust/benchmark/README.md), along with the measured baselines.
Scenario generation and JSON parsing happen outside the timed regions.
