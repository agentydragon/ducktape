# Execution input and ownership

The per-table lowerings in `sim.compiler` turn authored records, materialized paths,
jurisdiction rules and locations into the typed `sim.prepared` records a composed world
declares. Preparation does not fetch evidence, fit models, or reinterpret tax law. Each
declaration refuses the financial inputs it cannot execute where it is declared.

`sim.session` and `product.simulation` call `sim.world.World` directly with Python
records. Actions, claims, observations, mortgage servicing facts, and completed
results are not serialized through a private binding layer. The Python world
owns authoritative books, exact transactions, taxes, and contractual consequences;
the session owns path routing, monthly sequencing, component instances, and stops.

Public declarations, not an executing strategy, determine available account/asset
pools. An explicitly declared empty pool can be observed and purchased. Unsupported
prices, account references, distributions, or contract terms reject before execution.

## Serialization

JSON remains appropriate for explicit file and HTTP boundaries; in-process event and
metric projections consume completed records directly.

## Precision

Authored money must be exactly representable in its currency quantum. Sampled
prices round once to quanta; distribution rates preserve sub-quantum precision
until multiplied by holdings. Index levels use integer parts per billion.
Checks before quantization reject raw values whose invalidity rounding could hide.
Derived values use checked integer arithmetic and explicit rounding, not an
assumption that intermediate products are already exact currency quanta.
