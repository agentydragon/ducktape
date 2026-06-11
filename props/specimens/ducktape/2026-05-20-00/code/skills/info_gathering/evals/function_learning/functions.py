"""Secret functions for the function learning eval.

Each function is a concrete, hand-coded f: [0, max_input] → [0, max_output]
with known structure that an optimal learner can exploit. No randomization —
fully reproducible across runs.
"""

from abc import ABC, abstractmethod
from typing import ClassVar


class SecretFunction(ABC):
    """A secret function f: [0, max_input] → [0, max_output]."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Hint given to the model about the function class."""
        ...

    @property
    @abstractmethod
    def n(self) -> int:
        """Number of input bits. Inputs are integers in [0, 2^n - 1]."""
        ...

    @property
    @abstractmethod
    def m(self) -> int:
        """Number of output bits. Outputs are integers in [0, 2^m - 1]."""
        ...

    @property
    def max_input(self) -> int:
        return int(2**self.n - 1)

    @property
    def max_output(self) -> int:
        return int(2**self.m - 1)

    @abstractmethod
    def evaluate(self, x: int) -> int:
        """Compute f(x) where x is in [0, max_input]."""
        ...

    def all_inputs(self) -> list[int]:
        return list(range(self.max_input + 1))


# -- Linear function over GF(2): f(x) = Ax ⊕ b --
# Each output bit i = popcount(x & row_mask[i]) % 2  ⊕  bias[i].
# row_mask encodes the i-th row of A as a bitmask.


class LinearSimple(SecretFunction):
    """8→4 linear function over GF(2) with a fixed matrix A and bias b.

    Optimal strategy: query 0 (gives b) and the 8 powers of 2
    (each gives a column of A ⊕ b). 9 queries to fully determine f.
    """

    name = "linear_simple"
    description = "The function is linear over GF(2): f(x) = Ax ⊕ b for some binary matrix A and vector b."
    n = 8
    m = 4

    # Each entry: (row_mask, bias_bit). row_mask has bits set where A[row] is 1.
    # Row 0: [1,0,1,1,0,0,1,0] → 0b10110010 = 178, bias 1
    # Row 1: [0,1,0,1,1,0,0,1] → 0b01011001 = 89,  bias 0
    # Row 2: [1,1,0,0,0,1,1,0] → 0b11000110 = 198, bias 1
    # Row 3: [0,0,1,0,1,1,0,1] → 0b00101101 = 45,  bias 1
    _rows: ClassVar[list[tuple[int, int]]] = [(178, 1), (89, 0), (198, 1), (45, 1)]

    def evaluate(self, x: int) -> int:
        result = 0
        for i, (mask, bias) in enumerate(self._rows):
            bit = ((x & mask).bit_count() + bias) & 1
            result |= bit << (self.m - 1 - i)
        return result


# -- k-junta: depends on only k of n input bits --


class Junta3(SecretFunction):
    """8→4 function that depends on only bits 1, 4, 7 (0-indexed MSB-first).

    Optimal strategy: toggle individual bits to find the 3 relevant ones,
    then enumerate the 8 combinations of those bits.
    """

    name = "junta_3"
    description = "The function depends on only 3 of the 8 input bits. The other 5 bits are irrelevant."
    n = 8
    m = 4

    _relevant_bits: ClassVar[list[int]] = [1, 4, 7]
    # Indexed by the 3-bit value formed from relevant bits. sub_table[i] = output.
    _sub_table: ClassVar[list[int]] = [10, 6, 12, 1, 7, 9, 2, 15]

    def evaluate(self, x: int) -> int:
        sub_val = 0
        for bit_pos in self._relevant_bits:
            sub_val = (sub_val << 1) | ((x >> (self.n - 1 - bit_pos)) & 1)
        return self._sub_table[sub_val]


# -- Parity groups: each output bit is XOR of a disjoint pair --
# Output bit i = popcount(x & group_mask[i]) & 1.


class ParityGroups(SecretFunction):
    """8→4 function where each output bit is the XOR of a disjoint pair of input bits.

    Groups: {0,1}, {2,3}, {4,5}, {6,7}.
    Optimal strategy: query inputs that isolate each group.
    """

    name = "parity_groups"
    description = (
        "Each output bit is the XOR (parity) of a disjoint pair of input bits. "
        "The 8 input bits are partitioned into 4 pairs."
    )
    n = 8
    m = 4

    # Pair masks (MSB-first bit numbering):
    # bits {0,1} → 0b11000000 = 192, bits {2,3} → 0b00110000 = 48,
    # bits {4,5} → 0b00001100 = 12,  bits {6,7} → 0b00000011 = 3
    _pair_masks: ClassVar[list[int]] = [192, 48, 12, 3]

    def evaluate(self, x: int) -> int:
        result = 0
        for i, mask in enumerate(self._pair_masks):
            bit = (x & mask).bit_count() & 1
            result |= bit << (self.m - 1 - i)
        return result


# -- Variant registry --


LINEAR_SIMPLE = LinearSimple()
JUNTA_3 = Junta3()
PARITY_GROUPS = ParityGroups()


# -- 7-bit variants (128 inputs instead of 256) --


class Linear7(SecretFunction):
    """7->4 linear function over GF(2)."""

    name = "linear_7"
    description = "The function is linear over GF(2): f(x) = Ax + b for some binary matrix A and vector b."
    n = 7
    m = 4

    # Row 0: [1,0,1,1,0,0,1] → 0b1011001 = 89, bias 1
    # Row 1: [0,1,0,1,1,0,0] → 0b0101100 = 44, bias 0
    # Row 2: [1,1,0,0,0,1,1] → 0b1100011 = 99, bias 1
    # Row 3: [0,0,1,0,1,1,0] → 0b0010110 = 22, bias 1
    _rows: ClassVar[list[tuple[int, int]]] = [(89, 1), (44, 0), (99, 1), (22, 1)]

    def evaluate(self, x: int) -> int:
        result = 0
        for i, (mask, bias) in enumerate(self._rows):
            bit = ((x & mask).bit_count() + bias) & 1
            result |= bit << (self.m - 1 - i)
        return result


class Junta7(SecretFunction):
    """7->4 function depending on bits 1, 4, 6."""

    name = "junta_7"
    description = "The function depends on only 3 of the 7 input bits. The other 4 bits are irrelevant."
    n = 7
    m = 4

    _relevant_bits: ClassVar[list[int]] = [1, 4, 6]
    _sub_table: ClassVar[list[int]] = [10, 6, 12, 1, 7, 9, 2, 15]

    def evaluate(self, x: int) -> int:
        sub_val = 0
        for bit_pos in self._relevant_bits:
            sub_val = (sub_val << 1) | ((x >> (self.n - 1 - bit_pos)) & 1)
        return self._sub_table[sub_val]


class Parity7(SecretFunction):
    """7->4 function: 3 XOR pairs + 1 pass-through bit.

    Pairs: {0,1}, {2,3}, {4,5}. Bit 6 passes through as output bit 3.
    """

    name = "parity_7"
    description = (
        "Each of the first 3 output bits is the XOR of a disjoint pair of input bits. "
        "The 4th output bit depends on a single input bit."
    )
    n = 7
    m = 4

    # bits {0,1} → 0b1100000 = 96, bits {2,3} → 0b0011000 = 24,
    # bits {4,5} → 0b0000110 = 6,  bit {6} → 0b0000001 = 1
    _group_masks: ClassVar[list[int]] = [96, 24, 6, 1]

    def evaluate(self, x: int) -> int:
        result = 0
        for i, mask in enumerate(self._group_masks):
            bit = (x & mask).bit_count() & 1
            result |= bit << (self.m - 1 - i)
        return result


LINEAR_7 = Linear7()
JUNTA_7 = Junta7()
PARITY_7 = Parity7()


# -- Reed-Muller degree-3 polynomial over GF(2)^7 --
# Each output bit is the XOR of monomials of degree ≤ 3.
# Monomials are tuples of bit indices (0 = MSB). Empty tuple () = constant 1.
# Optimal strategy (linear probing) fails; higher-order differences are needed.


class ReedMuller3(SecretFunction):
    """7→4 degree-3 Boolean polynomial. Each output bit has at least one cubic term.

    Standard linear probing (query x=0 and powers of 2) does not recover the
    degree-2 and degree-3 terms. Discovering the full polynomial requires
    systematic higher-order finite-difference queries.
    """

    name = "rm3_7"
    description = (
        "The function is a Reed-Muller degree-3 polynomial over GF(2): each output bit "
        "is the XOR of Boolean monomials of degree at most 3 in the 7 input bits. "
        "There are C(7,1)+C(7,2)+C(7,3) = 63 possible non-constant monomials."
    )
    n = 7
    m = 4

    # Each row is a list of monomials. () = constant 1, (i,) = x_i, (i,j) = x_i·x_j, etc.
    # Row 0 (MSB): x0·x1·x2 ⊕ x3·x5 ⊕ x6 ⊕ 1
    # Row 1:       x1·x3·x5 ⊕ x0·x4 ⊕ x2
    # Row 2:       x0·x2·x4 ⊕ x1·x6 ⊕ x3·x5 ⊕ 1
    # Row 3 (LSB): x2·x4·x6 ⊕ x0·x3 ⊕ x1·x5 ⊕ x4
    _polynomials: ClassVar[list[list[tuple[int, ...]]]] = [
        [(0, 1, 2), (3, 5), (6,), ()],
        [(1, 3, 5), (0, 4), (2,)],
        [(0, 2, 4), (1, 6), (3, 5), ()],
        [(2, 4, 6), (0, 3), (1, 5), (4,)],
    ]

    def evaluate(self, x: int) -> int:
        result = 0
        for i, terms in enumerate(self._polynomials):
            bit = 0
            for monomial in terms:
                val = 1
                for idx in monomial:
                    val &= (x >> (self.n - 1 - idx)) & 1
                bit ^= val
            result |= bit << (self.m - 1 - i)
        return result


# -- Bent function: vectorial inner product --
# Split x into a = x >> 4 (top 4 bits) and b = x & 0xF (bottom 4 bits).
# Output bit i = inner_product(a, rot4(b, i)), where rot4 is a 4-bit left rotation.
# Each output bit is a bent Boolean function (maximally nonlinear).
# Standard linear probing recovers only zero information.


class BentInnerProduct(SecretFunction):
    """8→4 vectorial bent function based on the rotated inner product construction.

    f_i(a, b) = ⟨a, rot4(b, i)⟩ = popcount(a & rot4(b, i)) mod 2.
    Each component is a bent function: all Walsh-Hadamard coefficients equal +-16.
    Linear probing (query 0 and powers of 2) yields only 0 outputs and reveals
    nothing about the function -- the interactions are all multiplicative.
    """

    name = "bent"
    description = (
        "The function is a vectorial bent Boolean function. "
        "The input x is split into top half a = x >> 4 and bottom half b = x & 0xF. "
        "Output bit i is the inner product (mod 2) of a and a 4-bit left rotation of b by i positions. "
        "Bent functions are maximally nonlinear: standard linear probing does not apply."
    )
    n = 8
    m = 4

    @staticmethod
    def _rot4(b: int, i: int) -> int:
        """Left-rotate a 4-bit value by i positions."""
        return ((b << i) | (b >> (4 - i))) & 0xF

    def evaluate(self, x: int) -> int:
        a = x >> 4
        b = x & 0xF
        result = 0
        for i in range(self.m):
            bit = (a & self._rot4(b, i)).bit_count() & 1
            result |= bit << (self.m - 1 - i)
        return result


# -- Affine function over GF(2^8) --
# f(x) = (alpha * x XOR beta) & 0xF, where * is GF(2^8) multiplication
# using the AES irreducible polynomial x^8 + x^4 + x^3 + x + 1 (0x11b).
# alpha = 0x57, beta = 0x83. Output is the lower 4 bits of the full 8-bit product.
#
# GF(2^8) multiplication by a constant is a linear map over GF(2), so the
# function IS affine over GF(2). However, the 8x8 matrix has very specific
# structure: all entries are determined by the single byte alpha. An agent that
# knows GF(2^8) arithmetic can recover alpha in 2-3 queries; a naive linear
# probing approach needs ~8 queries to recover the same information.


class AESFieldAffine(SecretFunction):
    """8->4 affine function over GF(2^8) with the AES field polynomial.

    f(x) = (alpha * x XOR beta) mod 16, where * is GF(2^8) multiplication.
    alpha = 0x57, beta = 0x83 (both unknown to the model). Output is lower 4 bits.

    The function is affine over GF(2): f(x XOR y) = f(x) XOR f(y) XOR f(0).
    But the algebraic structure is far more compact than a generic affine map:
    only one byte (alpha) determines the entire linear component.
    """

    name = "aes_affine"
    description = (
        "The function computes f(x) = (alpha * x XOR beta) mod 16, where * denotes "
        "multiplication in GF(2^8) with the AES irreducible polynomial "
        "x^8 + x^4 + x^3 + x + 1, and alpha, beta in GF(2^8) are unknown constants. "
        "The output is the lower 4 bits of the full 8-bit result."
    )
    n = 8
    m = 4

    _alpha: ClassVar[int] = 0x57
    _beta: ClassVar[int] = 0x83
    # AES irreducible polynomial: x^8 + x^4 + x^3 + x + 1.
    _poly: ClassVar[int] = 0x11B

    @classmethod
    def _gf_mul(cls, a: int, b: int) -> int:
        """Multiply two GF(2^8) elements using the AES polynomial."""
        result = 0
        for _ in range(8):
            if b & 1:
                result ^= a
            a <<= 1
            if a & 0x100:
                a ^= cls._poly
            b >>= 1
        return result

    def evaluate(self, x: int) -> int:
        return (self._gf_mul(self._alpha, x) ^ self._beta) & self.max_output


REED_MULLER_3 = ReedMuller3()
BENT_INNER_PRODUCT = BentInnerProduct()
AES_FIELD_AFFINE = AESFieldAffine()

FUNCTIONS: dict[str, SecretFunction] = {
    # 8-bit (256 inputs).
    "linear_simple": LINEAR_SIMPLE,
    "junta_3": JUNTA_3,
    "parity_groups": PARITY_GROUPS,
    # 7-bit (128 inputs).
    "linear_7": LINEAR_7,
    "junta_7": JUNTA_7,
    "parity_7": PARITY_7,
    # Harder functions.
    "rm3_7": REED_MULLER_3,
    "bent": BENT_INNER_PRODUCT,
    "aes_affine": AES_FIELD_AFFINE,
}
