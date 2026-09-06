//! Property suite for the money kernel.
//!
//! `mul_div_round_half_up` is the single place every monetary figure in this engine
//! gets rounded, so its contract is checked against the definition of that contract --
//! nearest integer, ties away from zero -- rather than against a second copy of the
//! algorithm. The checks below multiply where the implementation divides, so a shared
//! mistake has nowhere to cancel out.

use proptest::prelude::*;

use crate::money::{
    ArithmeticError, Money, Quantity, mul_div_i128_round_half_up, mul_div_round_half_up,
};

/// Assert `quotient` is the integer nearest `product / denominator`, an exact tie
/// landing further from zero.
///
/// Undoing the division is what makes this independent of the implementation: the
/// leftover after multiplying back out is at most half a denominator, and where it is
/// exactly half -- the tie -- the rounded product sits no nearer zero than the exact one.
fn assert_half_away_from_zero(product: i128, denominator: i128, quotient: i128) {
    let scaled = quotient
        .checked_mul(denominator)
        .expect("a correctly rounded quotient times its denominator stays near the product");
    let residue = product - scaled;
    let twice_residue = residue
        .checked_abs()
        .and_then(|remainder| remainder.checked_mul(2))
        .expect("a rounding remainder is smaller than the denominator");
    assert!(
        twice_residue <= denominator.abs(),
        "{quotient} is not the nearest quotient of {product}/{denominator}"
    );
    if twice_residue == denominator.abs() {
        assert!(
            scaled.abs() >= product.abs(),
            "the tie at {product}/{denominator} rounded toward zero"
        );
    }
}

/// A lot's basis, its units, and cut points that sell it down to nothing.
///
/// Cuts stop short of the last unit, so the final piece is never empty and the sale
/// really does empty the lot.
fn lot_drawdown() -> impl Strategy<Value = (Money, Quantity, Vec<Quantity>)> {
    (1i64..=1_000_000, -1_000_000_000_000i64..=1_000_000_000_000).prop_flat_map(|(units, basis)| {
        proptest::collection::btree_set(1i64..units.max(2), 0..=8).prop_map(move |cuts| {
            let mut pieces = Vec::new();
            let mut sold = 0;
            for cut in cuts {
                pieces.push(Quantity(cut - sold));
                sold = cut;
            }
            pieces.push(Quantity(units - sold));
            pieces.retain(|piece| piece.0 > 0);
            (Money(basis), Quantity(units), pieces)
        })
    })
}

proptest! {
    /// Every accepted result is the correctly rounded one, and the only refusal the
    /// kernel is allowed is a true quotient no `i64` can hold.
    #[test]
    fn narrow_mul_div_rounds_half_away_from_zero(
        lhs: i64,
        rhs: i64,
        denominator in any::<i64>().prop_filter("the zero denominator has its own test", |d| *d != 0),
    ) {
        let product = i128::from(lhs) * i128::from(rhs);
        match mul_div_round_half_up(lhs, rhs, denominator, "proptest") {
            Ok(quotient) => {
                assert_half_away_from_zero(product, i128::from(denominator), i128::from(quotient));
            }
            Err(ArithmeticError::Overflow { .. }) => {
                let truncated = product / i128::from(denominator);
                prop_assert!(
                    truncated.abs() >= i128::from(i64::MAX),
                    "refused {product}/{denominator} = {truncated}, which fits in an i64"
                );
            }
            Err(other) => prop_assert!(false, "unexpected refusal: {other}"),
        }
    }

    /// Negating either side negates the result. Half away from zero is the rounding
    /// rule that has this property; half up and half even do not.
    #[test]
    fn narrow_mul_div_is_sign_symmetric(
        lhs in i64::MIN + 1..=i64::MAX,
        rhs in i64::MIN + 1..=i64::MAX,
        denominator in (i64::MIN + 1..=i64::MAX).prop_filter("nonzero", |d| *d != 0),
    ) {
        let Ok(quotient) = mul_div_round_half_up(lhs, rhs, denominator, "proptest") else {
            return Ok(());
        };
        prop_assume!(quotient != i64::MIN);
        prop_assert_eq!(mul_div_round_half_up(-lhs, rhs, denominator, "proptest"), Ok(-quotient));
        prop_assert_eq!(mul_div_round_half_up(lhs, -rhs, denominator, "proptest"), Ok(-quotient));
        prop_assert_eq!(mul_div_round_half_up(lhs, rhs, -denominator, "proptest"), Ok(-quotient));
    }

    /// A zero denominator is refused rather than panicking. The kernel is the sort of
    /// thing that gets swapped for a library one day; a library that returns `None`
    /// here invites an `unwrap` that would take down a whole rollout.
    #[test]
    fn a_zero_denominator_is_refused(lhs: i64, rhs: i64) {
        prop_assert_eq!(
            mul_div_round_half_up(lhs, rhs, 0, "proptest"),
            Err(ArithmeticError::DivisionByZero { operation: "proptest" })
        );
    }

    /// The wide kernel obeys the same contract, over operands whose product this check
    /// can still multiply back out inside an `i128`.
    #[test]
    fn wide_mul_div_rounds_half_away_from_zero(
        lhs in -(1i128 << 62)..=(1i128 << 62),
        rhs in -(1i128 << 62)..=(1i128 << 62),
        denominator in (-(1i128 << 62)..=(1i128 << 62)).prop_filter("nonzero", |d| *d != 0),
    ) {
        let quotient = mul_div_i128_round_half_up(lhs, rhs, denominator, "proptest")
            .expect("bounded operands cannot overflow");
        assert_half_away_from_zero(lhs * rhs, denominator, quotient);
    }

    /// Apportioning the whole of a lot moves the whole amount, exactly. This is the
    /// identity a per-unit basis could not offer, and the reason a piecewise sale adds
    /// back up.
    #[test]
    fn apportioning_everything_moves_everything(basis: i64, units in 1i64..=i64::MAX) {
        prop_assert_eq!(
            Money(basis).apportion(Quantity(units), Quantity(units), "proptest"),
            Ok(Money(basis))
        );
    }

    /// Selling a lot down in pieces consumes exactly the basis it held: nothing is
    /// stranded in the emptied lot, and nothing is spent that the lot never had.
    #[test]
    fn liquidating_a_lot_consumes_exactly_its_basis(
        (basis, units, pieces) in lot_drawdown()
    ) {
        let mut basis_remaining = basis;
        let mut units_remaining = units;
        for piece in pieces {
            let taken = basis_remaining
                .apportion(piece, units_remaining, "proptest")
                .expect("bounded operands cannot overflow");
            basis_remaining = basis_remaining.checked_sub(taken).expect("a share of the basis");
            units_remaining = Quantity(units_remaining.0 - piece.0);
            // Every intermediate state is one a lot could actually be in: the basis
            // shrinks toward zero and never crosses it.
            prop_assert!(
                basis_remaining.0.abs() <= basis.0.abs()
                    && basis_remaining.0.signum() * basis.0.signum() >= 0,
                "{basis_remaining:?} is not a remainder of {basis:?}"
            );
        }
        prop_assert_eq!(units_remaining, Quantity(0));
        prop_assert_eq!(basis_remaining, Money(0), "the emptied lot still holds basis");
    }
}
