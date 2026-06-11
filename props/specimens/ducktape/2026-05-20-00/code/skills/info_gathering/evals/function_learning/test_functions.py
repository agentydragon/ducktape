"""Unit tests for secret function implementations."""

import pytest_bazel

from skills.info_gathering.evals.function_learning.functions import (
    AES_FIELD_AFFINE,
    BENT_INNER_PRODUCT,
    JUNTA_3,
    LINEAR_SIMPLE,
    PARITY_GROUPS,
    REED_MULLER_3,
)


def test_linear_evaluates_in_range() -> None:
    for x in LINEAR_SIMPLE.all_inputs():
        out = LINEAR_SIMPLE.evaluate(x)
        assert 0 <= out <= LINEAR_SIMPLE.max_output


def test_linear_is_linear() -> None:
    """f(x XOR y) = f(x) XOR f(y) XOR f(0) for a linear function over GF(2)."""
    f0 = LINEAR_SIMPLE.evaluate(0)
    inputs = LINEAR_SIMPLE.all_inputs()
    for x in inputs[:16]:
        for y in inputs[:16]:
            fx = LINEAR_SIMPLE.evaluate(x)
            fy = LINEAR_SIMPLE.evaluate(y)
            fxy = LINEAR_SIMPLE.evaluate(x ^ y)
            assert fxy == fx ^ fy ^ f0, f"Linearity failed: f({x} ^ {y}) != f({x}) ^ f({y}) ^ f(0)"


def test_junta_depends_on_relevant_bits() -> None:
    """Flipping an irrelevant bit should not change the output."""
    irrelevant = [i for i in range(JUNTA_3.n) if i not in [1, 4, 7]]
    base_out = JUNTA_3.evaluate(0)
    for bit in irrelevant:
        flipped = 1 << (JUNTA_3.n - 1 - bit)
        assert JUNTA_3.evaluate(flipped) == base_out, f"Output changed when flipping irrelevant bit {bit}"


def test_junta_relevant_bits_matter() -> None:
    """Flipping a relevant bit should change output for at least some inputs."""
    relevant = [1, 4, 7]
    changes = 0
    base_out = JUNTA_3.evaluate(0)
    for bit in relevant:
        flipped = 1 << (JUNTA_3.n - 1 - bit)
        if JUNTA_3.evaluate(flipped) != base_out:
            changes += 1
    assert changes > 0, "No relevant bit flip changed the output"


def test_parity_groups_correct() -> None:
    """Each output bit should be XOR of its pair."""
    assert PARITY_GROUPS.evaluate(0b11000000) == 0b0000
    assert PARITY_GROUPS.evaluate(0b10000000) == 0b1000
    assert PARITY_GROUPS.evaluate(0b01000000) == 0b1000
    assert PARITY_GROUPS.evaluate(0b00110000) == 0b0000
    assert PARITY_GROUPS.evaluate(0b00100000) == 0b0100
    assert PARITY_GROUPS.evaluate(0b10101010) == 0b1111
    assert PARITY_GROUPS.evaluate(0b11111111) == 0b0000


def test_rm3_evaluates_in_range() -> None:
    for x in REED_MULLER_3.all_inputs():
        out = REED_MULLER_3.evaluate(x)
        assert 0 <= out <= REED_MULLER_3.max_output


def test_rm3_has_cubic_terms() -> None:
    """The 3rd-order finite difference w.r.t. the cubic-term variables must be 1.

    For output bit 0 (MSB), the cubic term is x0*x1*x2. Setting e_i = 1 << (n-1-i),
    the 3rd-order derivative D_{e0}D_{e1}D_{e2}f_0 must equal 1 (the coefficient).
    """
    n = REED_MULLER_3.n
    e0, e1, e2 = 1 << (n - 1), 1 << (n - 2), 1 << (n - 3)

    def f0(x: int) -> int:
        return REED_MULLER_3.evaluate(x) >> (REED_MULLER_3.m - 1)

    derivative = f0(0) ^ f0(e0) ^ f0(e1) ^ f0(e2) ^ f0(e0 ^ e1) ^ f0(e0 ^ e2) ^ f0(e1 ^ e2) ^ f0(e0 ^ e1 ^ e2)
    assert derivative == 1, "MSB output has no genuine degree-3 term"


def test_bent_evaluates_in_range() -> None:
    for x in BENT_INNER_PRODUCT.all_inputs():
        out = BENT_INNER_PRODUCT.evaluate(x)
        assert 0 <= out <= BENT_INNER_PRODUCT.max_output


def test_bent_known_values() -> None:
    """Spot-check specific input/output pairs derived from the inner product formula."""
    # a=0 or b=0 -> all inner products 0 -> output 0.
    assert BENT_INNER_PRODUCT.evaluate(0x00) == 0
    assert BENT_INNER_PRODUCT.evaluate(0x10) == 0  # a=1, b=0
    assert BENT_INNER_PRODUCT.evaluate(0x01) == 0  # a=0, b=1
    # a=0xF, b=0xF: every rotation of 0xF in 4 bits is still 0xF, popcount=4 (even) -> 0.
    assert BENT_INNER_PRODUCT.evaluate(0xFF) == 0
    # a=1, b=1: only i=0 rotation gives nonzero inner product (1&1=1, popcount=1 odd).
    assert BENT_INNER_PRODUCT.evaluate(0x11) == 0b1000


def test_bent_linear_probing_yields_nothing() -> None:
    """Querying 0 and all powers of 2 gives output 0 for every query.

    This confirms the standard linear probing strategy is useless: when either
    the top half (a) or the bottom half (b) of x is 0, every inner product is 0.
    """
    probes = [0] + [1 << i for i in range(BENT_INNER_PRODUCT.n)]
    for x in probes:
        assert BENT_INNER_PRODUCT.evaluate(x) == 0, f"Expected 0 for probe x={x:#x}"


def test_aes_affine_evaluates_in_range() -> None:
    for x in AES_FIELD_AFFINE.all_inputs():
        out = AES_FIELD_AFFINE.evaluate(x)
        assert 0 <= out <= AES_FIELD_AFFINE.max_output


def test_aes_affine_known_values() -> None:
    """Spot-check values derived from alpha=0x57, beta=0x83."""
    # f(0) = beta & 0xF = 0x83 & 0xF = 3.
    assert AES_FIELD_AFFINE.evaluate(0) == 3
    # f(1) = (alpha XOR beta) & 0xF = 0xD4 & 0xF = 4.
    assert AES_FIELD_AFFINE.evaluate(1) == 4
    # f(2) = (xtime(alpha) XOR beta) & 0xF = (0xAE XOR 0x83) & 0xF = 0x2D & 0xF = 13.
    assert AES_FIELD_AFFINE.evaluate(2) == 13


def test_aes_affine_is_affine() -> None:
    """f(x XOR y) = f(x) XOR f(y) XOR f(0) because GF(2^8) mult by a constant is linear."""
    f0 = AES_FIELD_AFFINE.evaluate(0)
    inputs = AES_FIELD_AFFINE.all_inputs()
    for x in inputs[:16]:
        for y in inputs[:16]:
            fx = AES_FIELD_AFFINE.evaluate(x)
            fy = AES_FIELD_AFFINE.evaluate(y)
            fxy = AES_FIELD_AFFINE.evaluate(x ^ y)
            assert fxy == fx ^ fy ^ f0, f"Affine property failed for x={x}, y={y}"


def test_all_functions_have_correct_dimensions() -> None:
    for fn in [LINEAR_SIMPLE, JUNTA_3, PARITY_GROUPS, BENT_INNER_PRODUCT, AES_FIELD_AFFINE]:
        assert fn.n == 8
        assert fn.m == 4
        assert len(fn.all_inputs()) == 256
        for x in fn.all_inputs():
            out = fn.evaluate(x)
            assert 0 <= out <= fn.max_output, f"{fn.name}: output {out} out of range for input {x}"
    assert REED_MULLER_3.n == 7
    assert REED_MULLER_3.m == 4
    assert len(REED_MULLER_3.all_inputs()) == 128


if __name__ == "__main__":
    pytest_bazel.main()
