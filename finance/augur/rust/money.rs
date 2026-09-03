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

    pub fn checked_neg(self) -> Result<Self, ArithmeticError> {
        self.0
            .checked_neg()
            .map(Self)
            .ok_or(ArithmeticError::Overflow {
                operation: "money negation",
            })
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
