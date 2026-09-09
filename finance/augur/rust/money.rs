use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Integer count of the scenario currency's declared quantum.
///
/// For a USD-cent scenario, `Money(123)` is $1.23. No binary floating-point
/// value may cross this boundary.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct Money(pub i64);

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct Quantity(pub i64);

/// A per-unit price or cost basis, counted in the same quanta as `Money`.
///
/// Distinct from `Money` because it is not an amount: it becomes one only when
/// multiplied by a `Units`, which carries the scale that per-unit figure is quoted
/// against. Conflating the two is how a lot's basis gets booked without its scale.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct PerUnit(pub i64);

/// A per-unit rate on the fixture's fine grid: `WIRE_RATE_SCALE` of these to one quantum.
///
/// A distribution is quoted per unit and can sit far below one quantum there -- a bond fund
/// at $56 a unit yielding 10bp pays under half a cent a unit a month -- while the amount it
/// comes to over a real position is ordinary money. Carrying it in whole quanta like a price
/// rounds away up to half a quantum per unit BEFORE the multiply by the position, and sends a
/// small enough rate to zero instead of preserving the amount earned (#5832).
///
/// A separate type from `PerUnit` rather than a second constructor on it, because the two are
/// different units and nothing else distinguishes them: an `i64` off the wire looks the same
/// either way, and the one thing that must never happen is a rate multiplied as if it were a
/// price -- off by a billion, silently, in the direction that overpays.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct PerUnitRate(pub i64);

/// A quantity together with the scale its integer counts in.
///
/// Asset scales differ -- a satoshi is not a share -- and the scale is a property of
/// the lot rather than of the arithmetic, so it travels with the number instead of
/// being passed beside it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Units {
    raw: i64,
    scale: i64,
}

impl Units {
    pub fn new(quantity: Quantity, scale: i64) -> Self {
        Self {
            raw: quantity.0,
            scale,
        }
    }

    pub fn quantity(self) -> Quantity {
        Quantity(self.raw)
    }
}

/// A dimensionless multiplier, as an exact rational.
///
/// One type for every multiplier this engine states, whatever unit it was authored in:
/// a rate off the wire, a fee in basis points, a literal quarter, or the ratio of two
/// levels of one series. The unit is a property of the constructor, not of the type,
/// so a call site reads what the multiplier means rather than which grid it came on.
///
/// Exact rather than fixed-point on purpose. A fixed scale forces every authored value
/// onto one grid and rounds whatever does not land on it; a rational carries `3/4` and
/// `1/360` exactly, and rounds once, where the product becomes money.
///
/// Deliberately not reduced to lowest terms. Every use multiplies through
/// `mul_div_round_half_up`, whose `i128` intermediate cannot overflow on `i64` operands,
/// so the `gcd` would cost the rollout loop and buy nothing.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Factor {
    numerator: i64,
    denominator: i64,
}

/// The one grid the fixture encodes every dimensionless value on -- rates, fractions and
/// index levels alike.
///
/// Past the boundary a `Factor` carries its own denominator, so only wire decoding and the
/// validators that bound a wire field against "one" have any business naming this.
pub const WIRE_RATE_SCALE: i64 = 1_000_000_000;

/// Whether `scale` is a quantity scale the engine will accept: a positive power of ten.
///
/// Every producer emits one -- a satoshi, a gwei, the default millionth of a unit -- and the
/// arithmetic assumes it: `Units` divides by the scale, and Augur reports a quantity as
/// `quanta / scale`, which is only a decimal figure a person can read back when the scale is
/// a power of ten. Accepting an arbitrary positive integer declared a domain wider than
/// anything writes or reads.
pub fn is_quantity_scale(scale: i64) -> bool {
    let mut remaining = scale;
    if remaining <= 0 {
        return false;
    }
    while remaining % 10 == 0 {
        remaining /= 10;
    }
    remaining == 1
}

impl Factor {
    pub const ONE: Self = Self {
        numerator: 1,
        denominator: 1,
    };

    pub const fn new(numerator: i64, denominator: i64) -> Self {
        Self {
            numerator,
            denominator,
        }
    }

    pub const fn percent(percent: i64) -> Self {
        Self::new(percent, 100)
    }

    pub const fn basis_points(basis_points: i64) -> Self {
        Self::new(basis_points, 10_000)
    }

    /// Decode a rate off the fixture, which spells every one of them on one integer grid.
    pub const fn parts_per_billion(parts: i64) -> Self {
        Self::new(parts, WIRE_RATE_SCALE)
    }

    /// What is left after taking this fraction away. Exact: only the numerator moves.
    pub fn complement(self, operation: &'static str) -> Result<Self, ArithmeticError> {
        self.denominator
            .checked_sub(self.numerator)
            .map(|numerator| Self::new(numerator, self.denominator))
            .ok_or(ArithmeticError::Overflow { operation })
    }

    /// This rate spread over `periods` equal periods -- an annual rate made monthly.
    ///
    /// Exact: dividing a rational by an integer scales its denominator, so the split takes
    /// no rounding of its own and the only one left is where the product becomes money. A
    /// fixed-point rate had to round the numerator back onto its grid here.
    pub fn per(self, periods: i64, operation: &'static str) -> Result<Self, ArithmeticError> {
        self.denominator
            .checked_mul(periods)
            .map(|denominator| Self::new(self.numerator, denominator))
            .ok_or(ArithmeticError::Overflow { operation })
    }

    pub fn numerator(self) -> i64 {
        self.numerator
    }

    pub fn denominator(self) -> i64 {
        self.denominator
    }
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum ArithmeticError {
    #[error("integer overflow during {operation}")]
    Overflow { operation: &'static str },
    #[error("division by zero during {operation}")]
    DivisionByZero { operation: &'static str },
}

impl Money {
    pub fn checked_add(self, rhs: Self) -> Result<Self, ArithmeticError> {
        self.0
            .checked_add(rhs.0)
            .map(Self)
            .ok_or(ArithmeticError::Overflow {
                operation: "money addition",
            })
    }

    pub fn checked_sub(self, rhs: Self) -> Result<Self, ArithmeticError> {
        self.0
            .checked_sub(rhs.0)
            .map(Self)
            .ok_or(ArithmeticError::Overflow {
                operation: "money subtraction",
            })
    }

    /// The share of this amount that `part` of `whole` accounts for.
    ///
    /// Apportionment, not a per-unit rate applied `part` times. The difference is that
    /// this leaves nothing behind: `part == whole` returns the whole amount exactly, so
    /// selling a lot down in pieces consumes its basis and no more. A per-unit figure
    /// derived once and multiplied cannot promise that -- its remainders need not sum
    /// back to the total, and truncating the derivation makes the shortfall one-sided.
    ///
    /// The quantity scale divides out, because both sides are counted in it.
    pub fn apportion(
        self,
        part: Quantity,
        whole: Quantity,
        operation: &'static str,
    ) -> Result<Self, ArithmeticError> {
        mul_div_round_half_up(self.0, part.0, whole.0, operation).map(Self)
    }

    /// This amount times a dimensionless multiplier, rounded half away from zero.
    ///
    /// The multiplier stays exact until here; this is the one rounding.
    pub fn scaled_by(
        self,
        factor: Factor,
        operation: &'static str,
    ) -> Result<Self, ArithmeticError> {
        mul_div_round_half_up(self.0, factor.numerator(), factor.denominator(), operation).map(Self)
    }

    /// What one unit of `units` costs, if this amount is what all of them cost.
    ///
    /// The inverse of `PerUnit::times`, and the operation that has to be spelled rather than
    /// stored: a per-unit figure derived once and multiplied back does not re-total, which is
    /// why a lot keeps its basis and reports this only when asked.
    pub fn per_unit(
        self,
        units: Units,
        operation: &'static str,
    ) -> Result<PerUnit, ArithmeticError> {
        mul_div_round_half_up(self.0, units.scale, units.raw, operation).map(PerUnit)
    }

    pub fn checked_neg(self) -> Result<Self, ArithmeticError> {
        self.0
            .checked_neg()
            .map(Self)
            .ok_or(ArithmeticError::Overflow {
                operation: "money negation",
            })
    }
}

impl Quantity {
    /// This quantity times a dimensionless multiplier, rounded half away from zero.
    ///
    /// The counterpart of `Money::scaled_by`: a forced-sale fraction takes a share of the
    /// units held the same way a tax rate takes a share of an amount.
    pub fn scaled_by(
        self,
        factor: Factor,
        operation: &'static str,
    ) -> Result<Self, ArithmeticError> {
        mul_div_round_half_up(self.0, factor.numerator(), factor.denominator(), operation).map(Self)
    }
}

impl PerUnit {
    /// The amount this per-unit figure comes to over `units`, rounded half away from
    /// zero. The scale divides out here because it travels on the `Units`.
    pub fn times(self, units: Units, operation: &'static str) -> Result<Money, ArithmeticError> {
        mul_div_round_half_up(self.0, units.raw, units.scale, operation).map(Money)
    }
}

impl PerUnitRate {
    /// The amount this rate comes to over `units`, rounded half away from zero -- ONCE, here.
    ///
    /// Both scales divide out together, and the product is formed before either does, so the
    /// only rounding in the path from a sampled rate to booked money is this one. The
    /// denominator is taken in `i128` because `units.scale * WIRE_RATE_SCALE` reaches 1e18 for
    /// a gwei-scaled asset, which is within `i64` but leaves no room to be casual about.
    pub fn times(self, units: Units, operation: &'static str) -> Result<Money, ArithmeticError> {
        let denominator = i128::from(units.scale) * i128::from(WIRE_RATE_SCALE);
        let amount = mul_div_i128_round_half_up(
            i128::from(self.0),
            i128::from(units.raw),
            denominator,
            operation,
        )?;
        i64::try_from(amount)
            .map(Money)
            .map_err(|_| ArithmeticError::Overflow { operation })
    }
}

/// Multiply two integers, divide by `denominator`, and round half away from
/// zero. The intermediate uses `i128`, so ordinary financial products cannot
/// overflow merely because their operands are `i64`.
pub fn mul_div_round_half_up(
    lhs: i64,
    rhs: i64,
    denominator: i64,
    operation: &'static str,
) -> Result<i64, ArithmeticError> {
    if denominator == 0 {
        return Err(ArithmeticError::DivisionByZero { operation });
    }
    let product = i128::from(lhs) * i128::from(rhs);
    let denominator = i128::from(denominator);
    let quotient = product / denominator;
    let remainder = product % denominator;
    let twice_remainder = remainder.abs() * 2;
    let rounded = if twice_remainder >= denominator.abs() {
        quotient + product.signum() * denominator.signum()
    } else {
        quotient
    };
    i64::try_from(rounded).map_err(|_| ArithmeticError::Overflow { operation })
}

/// Multiply two `i128` values, divide by `denominator`, and round half away
/// from zero. This is used for fixed-point contractual formulas whose scale is
/// wider than the persisted `i64` money boundary.
pub fn mul_div_i128_round_half_up(
    lhs: i128,
    rhs: i128,
    denominator: i128,
    operation: &'static str,
) -> Result<i128, ArithmeticError> {
    if denominator == 0 {
        return Err(ArithmeticError::DivisionByZero { operation });
    }
    let product = lhs
        .checked_mul(rhs)
        .ok_or(ArithmeticError::Overflow { operation })?;
    let quotient = product / denominator;
    let remainder = product % denominator;
    let twice_remainder = remainder
        .abs()
        .checked_mul(2)
        .ok_or(ArithmeticError::Overflow { operation })?;
    if twice_remainder >= denominator.abs() {
        quotient
            .checked_add(product.signum() * denominator.signum())
            .ok_or(ArithmeticError::Overflow { operation })
    } else {
        Ok(quotient)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn half_up_rounding_is_symmetric() {
        assert_eq!(mul_div_round_half_up(5, 1, 2, "test"), Ok(3));
        assert_eq!(mul_div_round_half_up(-5, 1, 2, "test"), Ok(-3));
        assert_eq!(mul_div_round_half_up(4, 1, 2, "test"), Ok(2));
    }

    #[test]
    fn wide_half_up_rounding_is_symmetric() {
        assert_eq!(mul_div_i128_round_half_up(5, 1, 2, "test"), Ok(3));
        assert_eq!(mul_div_i128_round_half_up(-5, 1, 2, "test"), Ok(-3));
    }

    /// 10,000 units of an asset held in millionths, which is what a fund position looks like.
    fn ten_thousand_units() -> Units {
        Units::new(Quantity(10_000 * 1_000_000), 1_000_000)
    }

    #[test]
    fn a_rate_of_whole_quanta_per_unit_agrees_with_a_price() {
        // The two types differ only in the grid they are quoted on, so where a rate happens to
        // land on a whole quantum they must come to the same money -- otherwise the split is a
        // behaviour change rather than a precision one.
        for quanta_per_unit in [1, 20, 4_237] {
            assert_eq!(
                PerUnitRate(quanta_per_unit * WIRE_RATE_SCALE)
                    .times(ten_thousand_units(), "test")
                    .unwrap(),
                PerUnit(quanta_per_unit)
                    .times(ten_thousand_units(), "test")
                    .unwrap()
            );
        }
    }

    #[test]
    fn a_rate_below_one_quantum_per_unit_still_comes_to_money() {
        // $0.0004 a unit over 10,000 units is $4.00. As a `PerUnit` this rate is not
        // expressible at all: it rounds to zero cents per unit and pays nothing (#5832).
        let rate = PerUnitRate(4 * WIRE_RATE_SCALE / 100);
        assert_eq!(rate.times(ten_thousand_units(), "test"), Ok(Money(400)));
        assert_eq!(PerUnit(0).times(ten_thousand_units(), "test"), Ok(Money(0)));
    }

    #[test]
    fn the_product_is_formed_before_either_scale_divides_out() {
        // One quantum spread across the WHOLE position: a ten-thousandth of a quantum per
        // unit, which is only representable because nothing rounds until the amount is money.
        assert_eq!(
            PerUnitRate(WIRE_RATE_SCALE / 10_000).times(ten_thousand_units(), "test"),
            Ok(Money(1))
        );
        // And the rounding is half away from zero AT THE AMOUNT rather than per unit, so half
        // a quantum over the position rounds up and a hair under it does not.
        assert_eq!(
            PerUnitRate(WIRE_RATE_SCALE / 20_000).times(ten_thousand_units(), "test"),
            Ok(Money(1))
        );
        assert_eq!(
            PerUnitRate(WIRE_RATE_SCALE / 20_000 - 1).times(ten_thousand_units(), "test"),
            Ok(Money(0))
        );
    }

    #[test]
    fn a_gwei_scaled_position_does_not_overflow_the_denominator() {
        // `units.scale * WIRE_RATE_SCALE` is 1e18 here, inside `i64` but with no room spare,
        // which is why the denominator is taken in `i128`.
        let units = Units::new(Quantity(3 * ETH_GWEI), ETH_GWEI);
        assert_eq!(
            PerUnitRate(7 * WIRE_RATE_SCALE).times(units, "test"),
            Ok(Money(21))
        );
    }

    const ETH_GWEI: i64 = 1_000_000_000;
}
