# Augur simulator design

This document describes the **current** `finance/augur/sim` implementation. It is not a
clean-room proposal. The financial capability surface lives in
[`REQUIREMENTS.md`](REQUIREMENTS.md).

`sim/` is the scenario model, the compiler that turns one into a plan, and the contract an
engine answers against. It does not contain an engine. The engine is
<../rust/README.md>, and `sim/` cannot import it — the dependency runs one way, which is what
keeps the compiled plan a description of the work rather than one engine's input format.

## Design goals

The simulator is a deterministic evaluator of typed financial scenarios over sampled
exogenous paths. Given the same scenario and sampled bundle, it produces the same result.
Its implementation is optimized for three properties:

1. exact integer accounting in configured currency quanta;
2. explicit financial semantics and ordering;
3. one canonical state/output representation, with read models projected at the boundary
   that needs them.

The implementation intentionally does **not** reconstruct state by replaying an event log.

## End-to-end pipeline

The production handoff is:

```text
product/API wire models
    -> product scenario translation
    -> Scenario
    -> compile_simulation(...)
    -> CompiledSimulation
    -> Engine.product_metrics(...) / run(...)
    -> canonical event frames and state channels
```

Each boundary changes representation for a concrete reason rather than mirroring the same
concept in two forms.

### Product/API translation

`finance/augur/product/scenarios.py` adapts the smaller user-facing `ScenarioKey` vocabulary
into the full authored `Scenario`. It creates the explicit agents, accounts, counterparties,
tax profiles, obligations, property cashflows, lifecycle events, and funding policies needed
by the domain model. Product models and simulator models are therefore related but not
interchangeable schemas.

### Authored scenario

`finance/augur/sim/scenario.py` owns the validated financial-domain model. Scheduled and
recurring transfers, property cashflows, purchases, sales, obligations, tax profiles, target
allocations, harvest policies, and private-equity policies remain explicit types. Their
validators reject invalid authored topology before compilation — a scenario that cannot be
built cannot be simulated, whichever engine would have run it. `scenario_test.py` states
that layer on its own, without executing anything.

### Compiler plan

`finance/augur/sim/compiler/` resolves the authored scenario into `CompiledSimulation`:
string tables, account and lot slots, per-asset quantity scales, the dense exogenous cubes in
integer quanta, and the deployment's tax law flattened into brackets, deductions and
exemptions. It imports only `jaxtyping` — annotations — and produces numpy.

Resolving tax law here rather than in an engine is deliberate. An engine that looked up its
own jurisdiction records could assess a different schedule than the one a case states, which
is how three divergences reached production before the plan became the single source.

`compile_simulation` also rejects a population of no rollouts: every engine reads the plan,
so the precondition belongs to the plan rather than to whichever entry point a caller used.

## The engine contract

`finance/augur/sim/backend.py` declares what an engine is. `CompiledRun` carries one
compiled plan and everything needed to execute it; `Engine` is the interface
`ProductService` holds — product metrics, the percentile fan, terminal summaries, and the
event log for one selected rollout.

Nothing above that contract knows which engine ran: the derived metrics, the terminal
reduction, the percentile brackets and the rollout projection are written once, against the
canonical event frames rather than against any engine's output layout.

## State and output contract

`finance/augur/sim/events.py` declares the canonical event frames, and
`sim/testing/simulation_result.py` declares the state channels — cash, lots, income, capital
gains, tax liabilities, properties, property stakes, liabilities and rollout status — with
the schema and sort key each carries. An engine projects its own run into those; a suite
reads them and never an engine's buffers.

State histories are indexed `snapshot = month + 1`, with month zero the compiled initial
condition rather than a replayed event frame.

## External series

`finance/augur/sim/external_series.py` is the consumer-side handoff. Evidence ingestion,
model fitting and sampling belong to `augur/model`; `sim/` is a deterministic path evaluator
once it receives those trajectories. The handoff is the model's own typed `LevelFrames` plus
the typed `PrivateEquityBundle`.

**Sampling stays on JAX, deliberately.** `model/gbm.py`, `state_space.py`, `vecm.py` and
`independent.py` run it, and a seed maps to sampled paths — re-implementing that PRNG
elsewhere would change every number a stored seed produces.

## Accounting and failure semantics

Money is integer quanta of the configured currency throughout. Obligations sharing one payer
and source account settle all-or-none. A rollout that cannot meet a hard demand stops: it
executes no further actions and reports the month it stopped, rather than continuing with a
negative balance.

A price that is not a price — zero, negative, non-finite — is refused where the series is
read, not absorbed. Valuing an unpriceable holding at zero would silently under-report net
worth and under-fund a cash band.

## Validation boundary

Authored topology is rejected by `scenario.py` validators. Everything derived from it —
missing series, unknown accounts, a sale exceeding its lots — is rejected during compilation
or by the engine reading the fixture, where the failure can name the row that caused it.

## Change discipline

- keep the compiled plan the single statement of what a run is, including its tax law;
- keep `sim/` free of any dependency on an engine package;
- state behaviour against the channels in `sim/testing/`, so a second engine is held to the
  same answers rather than to a second description of them.
