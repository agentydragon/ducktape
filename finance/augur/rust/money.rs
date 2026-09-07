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
    /// TODO(2026-09-06): divide the denominator instead, which is exact. Rounding the
    /// numerator onto the wire grid is what the fixed-point representation forced, and
    /// dropping it moves property-depreciation output, so it wants its own change.
    pub fn per(self, periods: i64, operation: &'static str) -> Result<Self, ArithmeticError> {
        mul_div_round_half_up(self.numerator, 1, periods, operation)
            .map(|numerator| Self::new(numerator, self.denominator))
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

    pub fn checked_neg(self) -> Result<Self, ArithmeticError> {
        self.0
            .checked_neg()
            .map(Self)
            .ok_or(ArithmeticError::Overflow {
                operation: "money negation",
            })
    }
}

impl PerUnit {
    /// The amount this per-unit figure comes to over `units`, rounded half away from
    /// zero. The scale divides out here because it travels on the `Units`.
    pub fn times(self, units: Units, operation: &'static str) -> Result<Money, ArithmeticError> {
        mul_div_round_half_up(self.0, units.raw, units.scale, operation).map(Money)
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
}
