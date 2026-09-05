# The fixture boundary

Everything that reaches the Rust engine crosses as one JSON document. `fixture.rs` declares
its shape and `fixture_encoder.py` writes it, and neither is derived from the other: the 139
distinct keys the encoder emits are, all 139 of them, field names declared on a Rust struct
in `fixture.rs`, `tax.rs`, `ledger.rs` or `money.rs`. The schema is written twice, by hand,
in two languages.

## Why there is a third model at all

The fixture is neither of the two models Augur already has. `Scenario` is what a user
configures — Pydantic, decimal money, optional everything. `CompiledSimulation` is what the
JAX engine runs — numpy cubes in integer quanta, tax tables already flattened to brackets.
The fixture is a third: integer units like the plan, but declarative like the scenario, and
a deliberate **subset** of both. `UnsupportedScenarioError` is the load-bearing part of that
subset — a scenario feature the fixture cannot express is refused, because a feature silently
dropped produces a fan that is wrong in a shape that looks right.

So the duplication is not an oversight of layering. It buys the subset, and the subset is
what makes a partial engine safe to run against a live request.

## What actually crosses

Very little of `fixture_encoder.py` is naming. The bulk is conversion the boundary genuinely
owes: `currency_amount_to_quanta` for configured money, `_round_ppb` for a level or rate that
JAX would quantize before multiplying integer money, `_exact_ppb` refusing a rate whose
decimal is finer than the wire, per-asset quantity scales, and `isinstance` narrowing of the
property lifecycle union into three flat lists. Those are the lines, and they are lines
either representation would need.

The repetition worth removing is the other kind: one shape spelled several times because
several scenario families share it. Transfers and property cashflows, one-shot or recurring,
differ only in their dates and whether a property is named, so `_flow` writes the rest once
and `_span` writes the window; `_obligation` does the same for the two obligation families.

## The Rust half, and why the sharing is on the Python side

`fixture.rs` carries the mirror-image repetition: four transfer-shaped structs, two
obligation ones. `#[serde(flatten)]` would share the common half, and it composes with
`deny_unknown_fields` — serde collects the leftover keys and rejects them
(`serde_derive` 1.0.229, `de/struct_.rs`). What changes is the parsing rather than the
strictness: the fields inside a flattened struct arrive through serde's content buffer
instead of the field visitor. `money_crosses_the_wire_only_as_an_integer` guards exactly
that, so flattening is a change whose one real risk is checked rather than assumed.

It still is not worth doing alone. Every read site in the engine gains a level —
`spec.cause_id` becomes `spec.flow.cause_id` across `cashflows`, `obligations`, `validation`
and `recorder` — which is a wide mechanical edit to a struct layout the end state below
deletes anyway.

## The exposure, and what guards it

`deny_unknown_fields` catches a key Rust does not know. It cannot catch the opposite: nearly
every list on `ScenarioSpec` is `#[serde(default)]`, so a field added on the Rust side and
never written by the encoder deserializes to its default in silence.

Nothing schematic closes that. What closes it is behavioral — a field Rust reads and the
encoder never writes makes Rust answer differently from JAX, which is what the differential
suites and `structural_fuzz_test` exist to find. A drift in this schema is therefore caught
as a disagreement about money rather than as a missing key, which is slower to read but
strictly stronger: it also catches the case where both sides spell the field and mean
different things by it.

## End state: the plan is the wire format

The fixture should stop being a third model. Both engines already run off `CompiledSimulation`
— `differential/fixture.py` hands Rust `encode_fixture` of the very plan JAX runs — so the
document Rust reads can be a serialization of that plan, and the encoder collapses to
serializing it.

This does not delete the subset, it moves it. Refusing an unmodeled feature becomes Rust's
job: the plan carries everything the JAX engine models, so Rust has to name what it will not
run instead of receiving a document from which it was already absent. That is the whole cost
of the change, and it is the reason it is a separate change rather than a rider on parity
work.

## Rejected: generate the encoder from the Rust structs

`schemars` on the `*Spec` structs would emit a JSON Schema, which could validate the
encoder's output in a test or generate typed Python models for it to fill. It closes the
silent-default hole above and it is not much work.

It is the wrong purchase anyway. It fixes the names, and the names are the cheap half — the
conversions stay hand-written and the same length, so the encoder does not shrink. What it
buys is a maintained codegen toolchain guarding the correctness of a model we intend to
remove, and a schema artifact that makes the third model harder to remove for having been
pinned. Spend the same effort on making the plan the wire format instead.
