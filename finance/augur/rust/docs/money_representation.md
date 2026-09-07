# Money representation

Money in this engine is an `i64` count of the scenario's declared currency quantum.
`Money(123)` is $1.23 under a one-cent quantum and CHF 1.15 under a five-rappen one;
the quantum itself never enters Rust. Every rounding happens in `mul_div_round_half_up`,
which multiplies in `i128` and rounds half away from zero, and its contract is pinned
by the property suite in `money_proptest.rs` rather than by sampled examples.

## Why not a decimal crate

A fixed-point or decimal crate is the obvious thing to reach for, and two were costed.
Neither fits, for the same reason:

**The quantum is not a decimal exponent.** `Currency.quantum` is an exact `Decimal`, and
CHF's five rappen and MGA's fifths are supported and tested. A decimal fixed-point type
parameterises on a power of ten, so it cannot represent a five-hundredths quantum at all.
Adopting one would mean narrowing the authoring surface to currencies whose subdivision
is a power of ten — a real loss of coverage, bought with a dependency.

**The engine has no scale to track anyway.** Scale-in-the-type is what these crates sell,
and it solves a problem this engine does not have: a `Money` here is dimensionless
inside the simulation. Its scale is fixed by the scenario and never changes under any
operation, because every monetary operation is money times a dimensionless `Factor` or
money apportioned by a ratio of like quantities. The only genuine scale is a lot's
`quantity_scale`, and it already travels in the type, on `Units`.

Specifics on the two that were costed:

- **`primitive_fixed_point_decimal`** is the closest fit — the fastest of the crates in
  its author's comparison, with an out-of-band runtime scale and a `checked_mul_ratio`
  matching this engine's `a × b / c` shape. Its `Rounding::Round` turns out to be half
  away from zero, symmetric in sign, computed by widening to `i128` and range-checking
  back: the same rule and the same method as `mul_div_round_half_up`. Adopting it would
  move no number, which is also the reason it buys nothing.
- **`rust_decimal`** is already a workspace dependency, but it is arbitrary-precision
  decimal, not a quantum count. Money would stop being an integer at the wire boundary
  that `fixture.rs` exists to keep integral.

## Why the multiplier is a rational and not fixed-point

`Factor` was three fixed-point types -- parts per billion, basis points, and a free ratio --
which is two more representations than a dimensionless multiplier needs, and it named a
grid where a reader wants a meaning. A rational states `3/4`, `1/360` and `6.375%` exactly,
so the only rounding is the one where the product becomes money.

Rational arithmetic was costed as a dependency and declined. `num-rational` reduces with a
`gcd` on every construction and three more per multiply, has no checked operations, and its
`round()` is the half-away-from-zero rule we already implement -- so it would move no number
while charging the rollout loop. Worse, the one place a rational looks like the answer is
where it fails: mortgage amortization compounds over 360 months, and a rational's terms grow
exponentially in the exponent. That path stays fixed-point at `CONTRACT_SCALE`, deliberately,
because wide fixed-point is the right representation for iterated multiplication.

## What stays a bare integer, and why

Thirteen call sites still hand `mul_div_round_half_up` three integers, and they are meant to.
They are not money times a multiplier; they are arithmetic _within_ the fixed-point rate
domain, where the operands are rates and the result is a rate:

- the TLH harvest curve -- an embedded-gain and a drawdown derived as rates, fed through
  `pow_half_ppb` and `mul_ppb`. Exponentiation is where a rational's terms explode, and the
  curve's inputs are clamped to the grid, so they belong on it.
- mortgage amortization at `CONTRACT_SCALE`, for the same reason over 360 periods.
- `pe_sellable_units`, which composes two rates: as a rational the numerator would be 10^18 at
  the entirely ordinary "100% of 100%", and overflow an `i64`.
- the mortgage-interest deduction, which accumulates across mortgages in `i128` and rounds
  once at the end rather than per mortgage.

The dividing line is whether the value becomes money at the end of the operation. Where it
does, a `Factor` states it and one rounding closes it. Where a rate is an input to more rate
arithmetic, fixed point is the right representation and the raw call is the honest spelling.

The rounding rule is not negotiable independently of this: half away from zero is what
makes negating an operand negate the result, which the tax and give-back paths rely on.
Half-even would break that symmetry.
