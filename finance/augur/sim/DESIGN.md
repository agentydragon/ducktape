# Augur simulator implementation

The current code has one canonical set of financial mechanics and two remaining
orchestration surfaces: the common monthly action session and the configured
runner used by the app, feature-rich benchmark and legacy acceptance readers.
They share financial steps but differ in policy control and supported domains.

## Preparation and dependencies

`Scenario` describes actors, accounts, holdings, contracts and path bindings.
`compile_run` in <backend.py> resolves those inputs into the typed `CompiledRun`
defined in <prepared.py>.
It does not fetch market evidence, fit a model or load tax law independently.
Prepared records own exact monetary terms, quantized market paths and variable-length
resolved tax rules. Sessions and metadata readers consume these facts directly;
the native/file boundary privately serializes them without retaining a parallel
document or original authoring objects. File decoding returns the same typed records.

`sim/` owns declarations and common books/results; `rust/` imports those Python
types at its result boundary. Preparation does not depend on the executor.
`model/` and `fit/` sample and fit; `policy/` contains proposal helpers.
Neither financial settlement nor preparation depends on the app's `product/`
or HTTP modules.

## Common experiment session

```text
authored scenario + supplied paths/rules
    -> compile_run
    -> ActionSession.start()
    -> current scoped observations
    -> Python batch policy
    -> ActionSession.advance(ordered actions)
    -> next observations or typed Finished
```

The caller owns the time loop and optional policy memory. Each path is stateful;
parallel paths do not make future months independent. Policies see current
actor-scoped facts, not future sampled market trajectories. The executor owns
phase ordering, validation, settlement, liabilities and tax consequences.

`rust/simulator.pyi` declares the current Python action boundary, while
`rust/engine/actors.rs` implements the retained session and its capability checks.
Configured allocators, housing and PE behavior are not silently enabled through
this API. A rejected action stops only its rollout with the successful prefix
intact; an unpaid due claim is a different stop reason. There is no retry callback
within the month.

## Books and capture

Money and quantities use their declared fixed-point scales. Canonical state is
not reconstructed by replaying event descriptions. `books.py` and `results.py`
define typed books, receipts, stops and completed results; `events.py` defines
the columnar event frames. Compact capture and selected dense/forensic capture
come from the same financial execution.

Opening snapshot zero precedes events. A stopped event month `f` has an ending
book at snapshot `f + 1`, marked at the already observed month `f`; no future
marks or decisions are invented. Reporting must retain this distinction when
comparing stopped books with completed horizons.

## Remaining configured consumers

`Engine` in <backend.py> and `RustEngine` in <../rust/backend.py> serve the
existing product methods. Their configured runner owns full-horizon loops and
implicit allocation/grouped-funding behavior. The app's projections do not define
the financial capabilities or output shape required by every experiment.

`sim/testing/simulation_result.py` and `rust/result.py` are the separate legacy
acceptance adapter, not the common public result contract. Existing tests on
those adapters remain until equivalent supported-domain controls move to the
action session. Keep independent expected financial facts, rather than retaining
an obsolete runner just to compare implementations.

The standalone feature-rich benchmark includes housing, PE, harvesting and
multiple obligated actors. Migrating public-only tests does not make that whole
workload compatible with a single-actor session or authorize stripping it down.
