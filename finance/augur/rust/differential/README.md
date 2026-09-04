# Rust/JAX differential harness

Both engines run the same authored scenario and answer in one shape, so a suite compares them
without knowing which produced which rows.

## One scenario, two engines

`case.py` is where a case is written. A `Case` is a `Scenario`, the sampled paths it runs over,
and the locations its properties sit in; it compiles that once and hands the same
`CompiledSimulation` to both sides — JAX executes it, `fixture_encoder.encode_fixture` encodes it
as the Rust simulator's integer document. Tax law reaches both engines only through those
compiled tables, resolved from the deployment's own jurisdiction records, so a case's brackets,
standard deduction, §1250 rate, capital-loss cap and interest exemptions cannot differ between
the engines.

That direction is the point. The integer fixture carries tax rules and the JAX engine never read
them — it resolved jurisdictions from `sim/data/jurisdictions/*.yaml` — so a fixture could state
a rule only one engine ever saw, and three divergences were traced to exactly that before the
authoring moved up to the scenario.

Only the Rust side has rules the scenario level cannot state: the encoding refuses a scenario the
integer document has no room for (a lot whose per-unit basis does not multiply out to whole
quanta, a rate finer than parts per billion). Those are `UnsupportedScenarioError`, tested
against `run_rust` rather than parameterized over both backends, because the JAX engine genuinely
has no such constraint.

## The layer

`backend.py` is the whole contract. `run_jax` and `run_rust` each take a `Case` and return a
`SimulationResult` whose channels use the canonical schemas `sim/testing/state_helpers.py`
defines; `assert_backends_agree(case)` runs both, compares every state channel and every canonical
event frame, and hands back the result for whatever the case is actually about.

A suite therefore reads:

```python
def test_backends_agree_on_grouped_recurring_obligations() -> None:
    result = assert_backends_agree(recurring_obligation_case())
    assert result.rollout_status.get_column("failed_month").to_list() == [1, 1]
```

Properties that should hold for either engine rather than between them —
that a malformed scenario is refused, say — parameterize over `BACKENDS` instead.

`run_rust` goes through the extension module, not the CLI, so both backends take a case and
nothing else. It asks for forensic output because the harness checks the balanced journal, which
has no JAX counterpart.

Rust emits the event frames already in Augur's column names and units (`event_frames.rs`),
so `output_adapter.py` translates nothing — it checks that the frames and columns which
arrived are the ones `sim/events.py` declares, and fails naming the frame when they are not.
State channels still need projecting, because how JAX chooses to report state is a fact
about JAX rather than about the units Rust holds it in; those projections are the
canonicalizations below.

## What is one engine's alone

`RustResult` carries the channels JAX has no equivalent for: the balanced journal, the TLH
deferral ledger, held bond principal, bond cashflows, distributions, the accrual fields
beyond `tax_breakdowns`, a property sale's tax split, and per-snapshot property detail. A
case wanting those asks the Rust result for them by name, so it is visible in the test that
only one engine is being read.

## Canonicalizations, and why each is not a fudge

Making the two meet turned up places where they say the same thing differently. Each is
handled in `backend.py` with the reason at the code, not inside a reader where the next
person would not find it:

- **Declared accounts.** Rust's ledger also carries the internal accounts double-entry needs
  — opening equity, asset basis, realized gain, tax expense, the external boundary — and JAX
  models none of them as cash. The cash channel is the scenario's declared accounts.
- **Unheld lots.** A preallocated target-allocation slot holds no units, and each engine
  puts a different placeholder in its basis and purchase month. Both are blanked while the
  lot is empty; what a lot cost and when it was bought is compared through
  `lot_dispositions`.
- **Zero capital-gain rows.** JAX masks gain rows by a per-tax-year active flag and Rust
  emits every snapshot, so only nonzero gains are compared — a zero row and an absent row
  say the same thing.

## Scope Rust does not cover

JAX tracks capital gains for any agent holding lots or selling; Rust surfaces them only for
an agent with a tax profile. The comparison is scoped to taxed agents. An untaxed agent's
gain has no tax consequence, so this is output coverage rather than a wrong number — but it
means a case's untaxed realizations go unchecked on the Rust side.
