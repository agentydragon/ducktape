"""Secret functions for the function learning eval.

Each function is a concrete, hand-coded f: [0, max_input] → [0, max_output]
with known structure that an optimal learner can exploit. No randomization —
fully reproducible across runs.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


_NO_HINT = "The function class is unknown. You must discover its structure from queries alone."


@dataclass(frozen=True)
class Variant:
    function: SecretFunction
    turn_limit: int
    description_override: str | None = None

    @property
    def function_description(self) -> str:
        """Description shown to the model — may hide the function class."""
        if self.description_override is not None:
            return self.description_override
        return self.function.description


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

VARIANTS: dict[str, Variant] = {
    # 8-bit, with hints.
    "linear_simple": Variant(function=LINEAR_SIMPLE, turn_limit=12),
    "junta_3": Variant(function=JUNTA_3, turn_limit=12),
    "parity_groups": Variant(function=PARITY_GROUPS, turn_limit=12),
    # 8-bit, without hints.
    "linear_nohint": Variant(function=LINEAR_SIMPLE, turn_limit=12, description_override=_NO_HINT),
    "junta_nohint": Variant(function=JUNTA_3, turn_limit=12, description_override=_NO_HINT),
    "parity_nohint": Variant(function=PARITY_GROUPS, turn_limit=12, description_override=_NO_HINT),
    # 7-bit (128 inputs), with hints.
    "linear_7": Variant(function=LINEAR_7, turn_limit=30),
    "junta_7": Variant(function=JUNTA_7, turn_limit=30),
    "parity_7": Variant(function=PARITY_7, turn_limit=30),
    # 7-bit, without hints.
    "linear_7_nohint": Variant(function=LINEAR_7, turn_limit=30, description_override=_NO_HINT),
    "junta_7_nohint": Variant(function=JUNTA_7, turn_limit=30, description_override=_NO_HINT),
    "parity_7_nohint": Variant(function=PARITY_7, turn_limit=30, description_override=_NO_HINT),
}
