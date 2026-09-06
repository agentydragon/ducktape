# What the Rust engine cannot do that the JAX engine can

The two engines answer the same scenarios in the same shapes, and the differential suite
proves they agree wherever both run. This is the complement: what the JAX engine models and
the Rust engine does not, so that a reader deciding whether Rust can serve a workload does
not have to derive the answer from a `UnsupportedScenarioError` traceback.

Each entry says where the boundary is enforced, because that is what has to move for the gap
to close. Two kinds:

- **Refused at the fixture** — `fixture_encoder.py` raises `UnsupportedScenarioError` rather
  than encoding a scenario feature the fixture cannot express, or a value it cannot represent
  exactly. Loud, and never silent.
- **Answered differently** — both engines run it and disagree. There is exactly one, and
  `differential/known_divergence_test.py` pins both answers.

Behaviour neither engine models is not a parity gap and is not here; those live in
[../../TODO.md](../../TODO.md) and [../../sim/TODO.md](../../sim/TODO.md) with the rest of
the modelling backlog.

A gap leaves this file when the capability lands, not when it is planned. Sequencing, gates
and what is reachable from a live product request are the plan's business
([../plans/rust_as_default.md](../plans/rust_as_default.md)), not this file's.

## Scenario features the fixture cannot express

| Feature                                            | Where JAX has it                                         | Refusal                                                                                                             |
| -------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| A scheduled sale at an authored price per unit     | `ScheduledAssetSale.price_per_unit`                      | Rust prices every sale off the asset's own sampled series, so a fixed price has nowhere to go.                      |
| An amount indexed by a series that is not an index | `SeriesIndexedAmount` over an arbitrary `LevelSeriesKey` | The fixture's `AmountSpec` indexes by inflation and rent levels only.                                               |
| A level series with no fixture representation      | any `LevelSeriesKey`                                     | Money series cross as currency quanta and index series as parts per billion; a key that is neither has no encoding. |

## Values that must be exactly representable

The fixture is strict integers throughout, so a float that does not land on the grid is
refused rather than rounded. Each of these is a `float`-typed knob in `Scenario` whose JAX
path carries it at full `float64` precision:

| Knob                                                           | Grid                                                            |
| -------------------------------------------------------------- | --------------------------------------------------------------- |
| `MortgageFinancing.annual_rate_pct`                            | parts per billion, after dividing by 100                        |
| `PropertySaleEvent.closing_cost_pct`                           | parts per billion, both the charged and the retained fraction   |
| `BondHolding.annual_coupon_rate` for an inflation-indexed bond | the period rate must be the exact PPB scaling of the annual one |
| `InitialLot` per-unit basis                                    | `basis × quantity_scale` must divide evenly by `units`          |

## Answered differently

**Which phases of a failing month were recorded.** A rollout that runs out of cash stops
inside Rust's month loop at the phase that could not pay, so whether an earlier phase is
recorded depends on where it sits in the order. JAX cannot leave a vectorized scan partway
through a month, so it records the whole failure month or none of it. No month-level rule
reproduces an ordering within one month, which is why this is a disagreement rather than an
off-by-one. `assert_results_agree` compares event frames outside the failure month, and
`differential/known_divergence_test.py` holds both engines' answers inside it.

## Output channels only one engine has

Not gaps in the same sense — neither engine is wrong — but a consumer written against one
will not find these in the other.

- **Rust only**: the balanced double-entry journal, the TLH give-back ledger, held bond
  principal, the §121 occupancy clock and depreciation accumulators, and the components
  behind a property sale's tax split. `RustResult` in `rust/result.py` names them.
- **JAX only**: nothing that is declared. Every channel in
  `sim/testing/simulation_result.py` is answered by both engines, and the JAX output arrays
  with no place there (`property_cumulative_depreciation`, `property_owner_occupied_months`)
  are ones Rust also keeps, in `property_details`.
