# Rust/JAX differential harness

Both engines run the same integer fixture and answer in one shape, so a suite compares them
without knowing which produced which rows.

## The layer

`backend.py` is the whole contract. `run_jax` and `run_rust` each take a fixture and return
a `SimulationResult` whose channels use the canonical schemas
`sim/testing/state_helpers.py` defines; `assert_backends_agree(fixture)` runs both, compares
every state channel and every canonical event frame, and hands back the result for whatever
the case is actually about.

A suite therefore reads:

```python
def test_backends_agree_on_grouped_recurring_obligations() -> None:
    result = assert_backends_agree(recurring_obligation_fixture())
    assert result.rollout_status.get_column("failed_month").to_list() == [1, 1]
```

Properties that should hold for either engine rather than between them —
that a malformed fixture is refused, say — parameterize over `BACKENDS` instead.

`run_rust` goes through the extension module, not the CLI, so both backends take a fixture
and nothing else. It asks for forensic output because the harness checks the balanced
journal, which has no JAX counterpart.

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
means a fixture's untaxed realizations go unchecked on the Rust side.

## Fuzzing the pair

The suites above are fixtures someone thought of. `generator.py` writes them at random, and
`value_fuzz_test.py` and `structural_fuzz_test.py` run them past the same oracle.

The cost model decides the design. JAX bakes the plan structure into the compiled program,
so what a fixture varies decides what it costs, and the generator splits its randomness to
match:

- a **shape** is everything reaching the XLA cache key — which policy families are present,
  how many of each, the horizon, the rollout count, and the thresholds and lifecycle months
  JAX folds in as Python scalars. A new shape costs a compile;
- a **value draw** is everything the compiled program takes as a traced input — opening
  balances, cashflow amounts and months, lot bases and quantities, sale months and units,
  tax brackets, and every external series. A new value draw over a fixed shape costs a run.

So the value tier runs many cases over a few fixed shapes and the structural tier runs few
cases over many shapes, and they are separate targets: they compile concurrently, and neither
process is left holding the other tier's executables, which JAX keeps for the life of a
process. `generator_test.py` pins the split: every fixture of one shape must
present the same entry counts, series axis and folded scalars, because a value draw that
moved one of those would silently turn the cheap tier into the expensive one.

### Where the values aim

Both engines are integer throughout, so a disagreement is a rounding-site or an ordering
difference, and a rounding site only has an opinion where the exact quotient falls on the
half. Uniform money never lands there — `price * units % 1_000_000 == 500_000` has
probability 1e-6 — so `rounding_boundary.py` solves for the operand that does, and the
generator aims it at the sites whose other operand it already knows: FIFO basis and sale
proceeds against the quantity scale, series-indexed amounts against their base level, bond
coupons against the period rate, and the quarterly estimated tax against its quarter.

Half the draws stay off the boundary, so the ordinary path keeps its coverage too.

### A finding is a fixture

`campaign.py` runs every case and then fails with one shrunk reproducer per **distinct**
differing channel, rather than stopping at the first: while one finding is open, stopping
there would make a second campaign say only what is already known. Shrinking drops scenario
entries, shortens the horizon, drops rollouts and flattens series, keeping every reduction
whose same channel still differs. Each minimal fixture goes to the test's undeclared outputs
as JSON, so it replays directly.

A case the legacy JAX surface cannot express — one fixed sale price per scheduled sale
across rollouts, a ppb rate that does not survive the float boundary — is counted apart from
the cases that were compared, and the compared count is what the test asserts. That is the
only number that is evidence of anything.

`known_divergence_test.py` holds what the fuzzer has found and nobody has resolved yet, one
pinned minimal fixture each. Nothing there is excused in the fuzzer: the fuzz targets fail on
those cases too, and an entry leaves the file when the engines are made to agree.

### Running it wider

```bash
bbr test //finance/augur/rust/differential:soak_test
```

Same generator, same oracle, wider seed ranges; `manual`, so `bazel test //...` skips it.
