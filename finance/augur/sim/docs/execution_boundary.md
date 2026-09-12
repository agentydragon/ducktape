# Execution input and ownership

`sim.compiler.execution.compile_run` prepares a typed `sim.prepared.CompiledRun`
from an authored scenario, materialized paths, jurisdiction rules, and locations.
Preparation does not fetch evidence, fit models, or reinterpret tax law.
`sim.validation` validates supported financial inputs before world construction.

`sim.session` and `sim.configured` call `sim.world.World` directly with Python
records. Actions, claims, observations, mortgage servicing facts, and completed
results are not serialized through a private binding layer. The Python world
owns authoritative books, exact transactions, taxes, and contractual consequences;
the session owns path routing, monthly sequencing, component instances, and stops.

Public declarations, not an executing strategy, determine available account/asset
pools. An explicitly declared empty pool can be observed and purchased. Unsupported
prices, account references, distributions, or contract terms reject before execution.

## Reproducible artifacts

`sim.artifacts.write_prepared_input` persists the typed prepared value.
`read_prepared_input` decodes it back into the same strict records. Money remains
integer currency quanta in the artifact; float money and unknown fields reject.
The compiler does not retain a second mutable input document or source scenario.

JSON remains appropriate for explicit file and HTTP boundaries. Configured
financial exports serve artifact readers and retained acceptance projections;
in-process event and metric projections consume completed records directly.

## Precision

Authored money must be exactly representable in its currency quantum. Sampled
prices round once to quanta; distribution rates preserve sub-quantum precision
until multiplied by holdings. Index levels use integer parts per billion.
Checks before quantization reject raw values whose invalidity rounding could hide.
Derived values use checked integer arithmetic and explicit rounding, not an
assumption that intermediate products are already exact currency quanta.
