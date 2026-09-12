# Library composition and metrics design gates

GWORLD is decided below and GMETRICS is still open; neither is a new API
specification. The [roadmap](roadmap.md#committed-library-cleanups-and-open-designs)
owns tasks and sequencing; [reader retirement](cleanup_migration.md) owns concrete
deletions. Existing code and interface sketches are evidence, not decisions the
open gate has already made.

## Agreed direction versus open mechanism

The agreed cleanups are:

1. Stop making `Scenario` / `PreparedScenario` / `CompiledRun` the mandatory way to
   compose every financial experiment. Retain useful validation, quantization and
   artifact import; an adapter must construct the same domain objects, not another
   financial executor.
2. Retire configured implicit-strategy orchestration consumer by consumer. Preserve
   supported financial behavior and explicitly resolve differences from ordered
   action execution before removing a reader.
3. Make stateful components composable without extending a central world schema or
   constructor switch for every new experiment. Keep one owner per financial fact.
4. Separate financial state and correctness from application-specific metrics and
   recording. The collection API and any role a coordinating World plays in it
   remain open.
5. Retire legacy artifact-to-frame acceptance projections while preserving their
   independent financial assertions.

The coordinating `World` below is the selected composition. Do not expose
arbitrary bookkeeping calls as the public API or install a universal
component/plugin framework.

The broader market-input/identifier redesign, package splitting and other parts
of the preceding audit were not confirmed by the operator's response. This update
does not promote those recommendations into new committed implementation tasks.
Previously recorded independent roadmap work retains its own scope.

## GWORLD — decided: a coordinating World over tracked components

Selected 2026-09-12. COMPOSE implements it; this section leaves with COMPOSE's
first landed slice, and its durable statements move to `SPEC.md` and
`sim/DESIGN.md`.

The experiment constructs an empty `World`, tracks the economic objects that take
part, then owns the loop around `World.step()`:

```python
# Target shape; names are not final API declarations.
world = World(paths=paths, tax_rules=rules)
retiree = SpendingHousehold(...)  # EconomicAgent subclass; its state lives on the instance
world.track(retiree)
mortgage = Mortgage(...)  # a contract that already exists at month zero
world.track(mortgage)

while not world.finished:
    metrics.append({"mortgage_remaining": world.principal(mortgage), ...})
    if some_condition:
        retiree.change_policy(...)  # experiment-owned logic between months
    world.step()
```

- `EconomicAgent` is a base class the experiment subclasses. Spending tiers,
  memory and parameters live on the instance. `World.step()` calls each tracked
  agent's `decide(observation)` exactly once per month, after scheduled flows and
  due-claim assembly, then executes the returned ordered actions and closes the
  month. The once-per-month, caller-ordered, fatal-rejection contract is unchanged;
  only the invoker of `decide` moves from the experiment's loop into `step()`.
- Tracking closes before the opening snapshot. `track()` is where cross-object
  consistency is checked: unknown accounts, duplicate ownership, cross-actor
  references, missing price series. Attaching mid-run is contract origination and
  stays with GP/GHOUSE.
- A domain the experiment did not track is absent from the world, observations,
  results and fixtures, not present-and-empty. A two-security spending experiment
  produces no private-equity, property, bond or TLH fields anywhere.
- Financial facts keep one owner. Outstanding principal stays in the ledger; a
  `Mortgage` reads it through the world rather than mirroring it. Components expose
  readings, never a second authoritative book.
- The single-path world is the primary object. A batched N-path driver (today's
  `ActionSession`) becomes a layer over N worlds for vectorised policies, later and
  not as a second policy interface. Selected replay re-creates agents with fresh
  state on the same paths.
- Contracts that begin from an agent's decision (a purchase originating a mortgage)
  wait for GHOUSE; servicing a contract that exists at month zero is in scope.

Rejected: explicit guarded composition without a World. Consistency checks across
agents, contracts and tax treatment need one registration point, and every sketch
of the lighter form reinvented it. A separable state-container/step-coordinator
hybrid is deferred until a consumer needs the split; forwarding alone does not
justify it.

### COMPOSE evidence

Small runnable compositions on supplied paths, not a framework spike:

- A spending agent with cash/lots and explicit tax treatment, including a taxable
  sale, due claim and year boundary.
- An opaque TLH component with contribution/withdrawal and settlement rejection.
- An already-supported mortgage servicing example with ledger-authoritative
  principal and paid-interest state; no new housing-purchase policy is required.

Exercise unknown/cross-actor references, duplicate component ownership, invalid
transfers, insufficient funding, a rejected middle action, an unpaid tax claim,
explicit untaxed treatment, repeated/omitted lifecycle calls and independent
rollouts. Check independently calculated books and unchanged established behavior.
An omitted duty must reject or produce an explicit incomplete/failure result,
not silently certify the period. Checkpoints, nested forecasts, a many-agent
economy and a throughput target are not prerequisites.

## GMETRICS — experiment-owned measurements and recording

**Agreed responsibility:** experiments and applications choose their measurements;
financial state does not grow a universal app-specific metric tuple.
**Unresolved mechanism:** how observations, optional history and collectors are
exposed and where collection is orchestrated.

The operator's pull-style example is a candidate:

```python
# Illustrative only: neither this account API nor a collector API is finalized.
metrics.append({"account_balance": accounts["foo"].balance})
```

Compare it with optional observers/recorders over committed outcomes and a hybrid
of minimal per-step outcomes plus caller-authored pull measurements. A World may
provide coherent observation points or invoke observers; that does not make it
owner of the experiment's measurement definitions. Do not select a mandatory
callback, event bus, subscription protocol or global metric registry in advance.

The comparison must settle:

- Opening, pre-decision, post-settlement and closing measurement times; currency,
  quantity and inflation bases; stable actor/path identity.
- How recording distinguishes policy intent, attempted actions, payments, unpaid
  liabilities, observed zeros and unobserved post-stop periods.
- Read-only/copy-safe observations: appending a row must not accidentally retain
  mutable live state or mutate financial books. Policy views cannot gain future
  path access merely because the experiment records broader outcomes.
- Which receipts/bookkeeping facts are required for financial correctness and
  which histories or derived metrics are optional. Disabling metrics must not
  disable taxes, settlement checks or visible failure state.
- How optional recording avoids duplicate financial execution, replayed tax/basis
  calculations, and mandatory whole-horizon capture for a per-step consumer.
  Collector errors are not silently classified as investment ruin.

Compare two consumers using the same execution: a small per-step experiment
recording selected values and the existing product fan/detail projection. Check
stopped paths, exact monetary values, selected replay and unchanged financial
outcomes with collection on/off. No benchmark contest is required to decide
ownership, though obvious unnecessary copies should be identified.

Close GMETRICS only after the operator chooses a scoped observation/collection
contract. RECORD then migrates the affected collectors and projections atomically.
If a chosen recording API depends on new lifecycle hooks, that slice also needs
the relevant COMPOSE step boundary. Existing typed-result reader cleanup and CAP's
specific missing financial observations do not wait for either entire redesign.
