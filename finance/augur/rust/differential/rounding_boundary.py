"""Operands that land a `mul_div_round_half_up` site exactly on the tie.

Both engines hold money in integer quanta, so a difference between them is a rounding-site
or an ordering difference rather than drifting floats. A rounding site only has an opinion
where the exact quotient falls precisely halfway — `lhs * rhs / denominator` with remainder
`denominator / 2` — and everywhere else both engines truncate to the same integer no matter
how the tie-break is written.

Uniform random money essentially never lands there: with a millionth-of-a-unit quantity
scale, `price * units % 1_000_000 == 500_000` has probability 1e-6. So the generator solves
for the operand instead of sampling and hoping.
"""

from math import gcd


def half_way_operand(multiplier: int, denominator: int, *, near: int, minimum: int = 1) -> int | None:
    """The smallest-distance `x >= minimum` with `x * multiplier % denominator == denominator / 2`.

    `None` when the site can never tie: an odd denominator has no exact half, and a
    `multiplier` sharing a factor with `denominator` that the half does not reaches only a
    sublattice of remainders.
    """

    if denominator <= 0:
        raise ValueError(f"denominator must be positive; got {denominator=}")
    if denominator % 2:
        return None
    target = denominator // 2
    divisor = gcd(abs(multiplier), denominator)
    if target % divisor:
        return None
    modulus = denominator // divisor
    root = (target // divisor) * pow(multiplier // divisor, -1, modulus) % modulus
    # The solutions are `root + k * modulus`; take the one closest to `near`, then walk up
    # to `minimum` — a caller asking for a positive amount does not want the zero solution.
    steps = (near - root + modulus // 2) // modulus
    solution = root + steps * modulus
    if solution < minimum:
        solution += -((solution - minimum) // modulus) * modulus
    return solution
