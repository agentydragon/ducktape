"""What the tolerant payload decode does with a value, including one its vocabulary does not hold."""

from __future__ import annotations

from enum import StrEnum

import pytest_bazel

from util.enum_vocab import UnknownValue, member_or_unknown


class Fruit(StrEnum):
    APPLE = "apple"
    PEAR = "pear"


def test_a_payload_value_decodes_to_its_member_or_a_named_unknown() -> None:
    assert member_or_unknown(Fruit, "apple") == "apple"
    assert member_or_unknown(Fruit, "durian") == UnknownValue("durian")


def test_a_payload_value_a_reader_could_not_name_survives_revalidation() -> None:
    """Re-validating an already-decoded payload must not double-wrap or drop the unknown."""
    assert member_or_unknown(Fruit, UnknownValue("durian")) == UnknownValue("durian")


def test_a_non_string_payload_value_is_left_for_the_callers_own_validation() -> None:
    assert member_or_unknown(Fruit, 7) == 7


if __name__ == "__main__":
    pytest_bazel.main()
