# Library composition and metrics design gates

These are unresolved design decisions for the agreed library cleanups, not a new
API specification. The [roadmap](roadmap.md#committed-library-cleanups-and-open-designs)
owns tasks and sequencing; [reader retirement](cleanup_migration.md) owns concrete
deletions. Existing code and interface sketches are evidence, not decisions that
these gates have already made.

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

A coordinating `World` is a viable design, not an antipattern merely because it
exists. An experiment can own its outer loop while calling `World.step()` to
advance registered economic objects and enforce cross-object consistency. A
lighter coordinator or explicit guarded-period composition is also viable. Do
not infer approval to remove World, expose arbitrary bookkeeping calls as the
public API, or install a universal component/plugin framework.

The broader market-input/identifier redesign, package splitting and other parts
of the preceding audit were not confirmed by the operator's response. This update
does not promote those recommendations into new committed implementation tasks.
Previously recorded independent roadmap work retains its own scope.

## GWORLD — composition, lifecycle and invariant boundary

**Open question:** what public object or protocol owns the composed economic state,
its lifecycle, and checks across all involved actors/components?

Compare at least these concrete alternatives:

- **Coordinating World:** the experiment constructs components, supplies them to a
  World, then owns a loop calling its guarded step operation. World may own or
  register objects and orchestrate their required advances.
- **Explicit guarded composition:** the experiment composes components with shared
  accounting/settlement and a small period coordinator; no mandatory all-owning
  World class. Guarded completion still enforces financial duties.
- **Hybrid:** a state/ownership container plus a separable step coordinator, only
  if the separation has a demonstrated consumer rather than adding forwarding.

The comparison must answer:

- Which state is owned, registered, shared read-only or recreated per rollout?
  How are accidental shared mutable components and multiple writers prevented?
- Where are all participating actors, accounts, counterparties, contract links and
  tax treatments checked for consistency, including newly attached components?
  A balanced journal alone does not establish an economically valid transaction.
- Who ensures claims, contractual payments, recorded tax consequences and year
  boundaries cannot be silently skipped? Who commits component state after
  settlement, and who determines the visible failure/stop outcome?
- Which observation/action/close boundaries can the experiment call, inspect or
  stop at? What prevents duplicate steps, stale claims and partially completed
  periods being reported as completed? Preserve current timing until a scoped
  change is explicitly decided.
- How do the existing single batch policy contract, actor-scoped observations and
  original rollout identities fit? Experiment visibility and policy visibility
  need not be identical. Do not invent a second policy interface as a side effect.
- How does legacy Scenario import instantiate the same supported objects without
  becoming a second authoring authority or financial implementation?

### Bounded evidence and closure

Use small runnable compositions on supplied paths, not a large framework spike:

- A spending policy with cash/lots and explicit tax treatment, including a taxable
  sale, due claim and year boundary.
- An opaque TLH component with contribution/withdrawal and settlement rejection.
- An already-supported mortgage servicing example with ledger-authoritative
  principal and paid-interest state; no new housing-purchase policy is required.

Exercise unknown/cross-actor references, duplicate component ownership, invalid
transfers, insufficient funding, a rejected middle action, an unpaid tax claim,
explicit untaxed treatment, repeated/omitted lifecycle calls and independent
rollouts. Check independently calculated books and unchanged established behavior.
An omitted duty must reject or produce an explicit incomplete/failure result,
not silently certify the period. Document which global and component-local checks
are guaranteed by each candidate.

Close GWORLD only after the operator selects the scoped ownership/step contract
from that evidence and its trade-offs. Record rejected alternatives briefly;
implementation belongs to COMPOSE. A current `World` class or a passing existing
suite does not close this public-design gate by itself. Checkpoints, nested
forecasts, a many-agent economy and a throughput target are not prerequisites.

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
the relevant GWORLD decision. Existing typed-result reader cleanup and CAP's
specific missing financial observations do not wait for either entire redesign.
