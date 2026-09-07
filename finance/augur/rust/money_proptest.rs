//! Property suite for the money kernel.
//!
//! `mul_div_round_half_up` is the single place every monetary figure in this engine
//! gets rounded, so its contract is checked against the definition of that contract --
//! nearest integer, ties away from zero -- rather than against a second copy of the
//! algorithm. The checks below multiply where the implementation divides, so a shared
//! mistake has nowhere to cancel out.

use proptest::prelude::*;

use crate::money::{
    ArithmeticError, Factor, Money, PerUnit, Quantity, Units, is_quantity_scale,
    mul_div_i128_round_half_up, mul_div_round_half_up,
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

/// Denominators that reach the rounding boundary as well as the extremes.
///
/// A uniformly random `i64` denominator is so large that a random dividend never lands
/// near half of it, so a suite drawn only from the full range checks the easy interior
/// of the rule and never its edge.
fn interesting_denominator() -> impl Strategy<Value = i64> {
    prop_oneof![
        2 => 1i64..=64,
        2 => -64i64..=-1,
        1 => any::<i64>().prop_filter("the zero denominator has its own test", |d| *d != 0),
    ]
}

/// Operands whose exact quotient is a half -- the case the rounding rule exists for.
///
/// Ties cannot be found by sampling, so they are constructed: `half` is one half of an
/// even denominator, and the dividend is that much past a whole multiple of it.
fn exact_tie() -> impl Strategy<Value = (i64, i64, i64)> {
    (1i64..=(1 << 30), -(1i64 << 30)..=(1 << 30), any::<bool>()).prop_map(
        |(half, multiple, below)| {
            let denominator = 2 * half;
            let offset = if below { -half } else { half };
            (multiple * denominator + offset, 1, denominator)
        },
    )
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
        denominator in interesting_denominator(),
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

    /// An exact tie lands away from zero, in both signs and in either direction across
    /// the multiple. This is the half of the rule that sampling cannot reach.
    #[test]
    fn an_exact_tie_rounds_away_from_zero((lhs, rhs, denominator) in exact_tie()) {
        let quotient = mul_div_round_half_up(lhs, rhs, denominator, "proptest")
            .expect("constructed operands cannot overflow");
        let product = i128::from(lhs) * i128::from(rhs);
        assert_half_away_from_zero(product, i128::from(denominator), i128::from(quotient));
        prop_assert!(
            i128::from(quotient).abs() * i128::from(denominator).abs() > product.abs(),
            "the tie at {lhs}/{denominator} did not move away from zero"
        );
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

    /// A factor states one number, however it was spelled. Equal rationals scale money
    /// identically, which is what makes moving a wire field between grids safe: the kernel
    /// rounds the exact product, and equal rationals have one exact product.
    #[test]
    fn equal_factors_scale_money_identically(
        amount: i64,
        basis_points in 0i64..=10_000,
    ) {
        let as_authored = Factor::basis_points(basis_points);
        let on_the_wire = Factor::parts_per_billion(basis_points * 100_000);
        prop_assert_eq!(
            Money(amount).scaled_by(as_authored, "proptest"),
            Money(amount).scaled_by(on_the_wire, "proptest")
        );
    }

    /// A factor and its complement split an amount with nothing created or lost beyond the
    /// one rounding each side takes. Both sides of every rented/owner split ride on this.
    #[test]
    fn a_factor_and_its_complement_split_an_amount(
        amount in -1_000_000_000_000i64..=1_000_000_000_000,
        parts in 0i64..=1_000_000_000,
    ) {
        let factor = Factor::parts_per_billion(parts);
        let taken = Money(amount).scaled_by(factor, "proptest").unwrap();
        let left = Money(amount)
            .scaled_by(factor.complement("proptest").unwrap(), "proptest")
            .unwrap();
        let recombined = taken.checked_add(left).unwrap();
        prop_assert!(
            (recombined.0 - amount).abs() <= 1,
            "{taken:?} + {left:?} is not {amount} back, give or take the two roundings"
        );
    }

    /// Over a whole number of units, the per-unit of a total is the price it came from.
    ///
    /// Only where the division is exact, which whole units make it. A fractional holding
    /// generally has no exact per-unit figure at all -- which is why a lot keeps its basis
    /// and derives this only to report it.
    #[test]
    fn per_unit_inverts_times_over_whole_units(
        price in -1_000_000_000i64..=1_000_000_000,
        whole_units in 1i64..=1_000,
        scale in prop_oneof![Just(100i64), Just(1_000_000), Just(100_000_000)],
    ) {
        let units = Units::new(Quantity(whole_units * scale), scale);
        let total = PerUnit(price).times(units, "proptest").unwrap();
        prop_assert_eq!(total, Money(price * whole_units));
        prop_assert_eq!(total.per_unit(units, "proptest").unwrap(), PerUnit(price));
    }

    /// Scaling a quantity is the same arithmetic as scaling money, so it rounds the same way.
    /// The engine takes a forced-sale share of units this way and a tax share of an amount
    /// the other; nothing about the two should differ.
    #[test]
    fn a_quantity_scales_like_money(amount: i64, parts in 0i64..=1_000_000_000) {
        let factor = Factor::parts_per_billion(parts);
        prop_assert_eq!(
            Quantity(amount).scaled_by(factor, "proptest").map(|q| q.0),
            Money(amount).scaled_by(factor, "proptest").map(|m| m.0)
        );
    }

    /// The quantity scales the engine accepts are exactly the powers of ten. A scale is a
    /// decimal shift: a quantity reads back as `quanta / scale`, which is only a figure a
    /// person can check when the divisor is one.
    #[test]
    fn only_powers_of_ten_are_quantity_scales(exponent in 0u32..=18) {
        prop_assert!(is_quantity_scale(10i64.pow(exponent)));
    }

    #[test]
    fn a_scale_that_is_not_a_power_of_ten_is_refused(scale in 1i64..=100_000) {
        let mut remaining = scale;
        while remaining % 10 == 0 {
            remaining /= 10;
        }
        prop_assume!(remaining != 1);
        prop_assert!(!is_quantity_scale(scale), "{scale} is not a power of ten");
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
