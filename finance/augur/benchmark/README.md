# Augur benchmark

The feature-rich scenario the engines are measured on, and the JAX driver that measures one.

`scenario.py` authors it as a `Scenario` and its sampled paths rather than as any engine's own
input format, so every engine runs one compiled plan — see
[../sim/testing/case.py](../sim/testing/case.py) for why that direction is the only one that
keeps a tax rule from reaching one engine and not another. Independent agents are combined
deliberately: the scenario exercises the supported policy surface without one policy family
starving another's liquidity.

The canonical shape is 60 monthly transitions plus the month-zero snapshot, a configurable
rollout count, 16 modeled cash accounts, scheduled and recurring transfers, deductions and
obligations, allocation and private-equity and TLH lots, four par-only bond/TIPS holdings, 17
row-major exact external series, and a 60-month property, mortgage, residency, rental,
improvement and sale lifecycle.

```bash
bb run //finance/augur/benchmark:jax_driver_bin -- --rollouts 500 --horizon-months 60
```

Scenario generation and JSON parsing happen outside the timed regions.
