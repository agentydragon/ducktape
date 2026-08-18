"""What the enum column types do with a value, including one their vocabulary does not hold."""

from __future__ import annotations

from enum import StrEnum

import pytest
import pytest_bazel

from util.sqlalchemy_types import TextBackedStrEnumUnionColumn, TolerantTextBackedStrEnumUnionColumn, UnknownValue


class Fruit(StrEnum):
    APPLE = "apple"
    PEAR = "pear"


class Vegetable(StrEnum):
    LEEK = "leek"


class AlsoFruit(StrEnum):
    APPLE = "apple"


def test_a_value_two_vocabularies_claim_is_refused_at_construction() -> None:
    """A reader could not tell which category the row is in, so the column will not be built."""
    with pytest.raises(ValueError, match="'apple' is in both"):
        TextBackedStrEnumUnionColumn(Fruit, AlsoFruit)


def test_the_strict_column_raises_on_a_value_no_release_of_it_ever_wrote() -> None:
    """The behaviour a decision vocabulary keeps: nothing may act on a value it cannot name, so the
    read fails rather than handing a caller something to guess with."""
    with pytest.raises(KeyError):
        TextBackedStrEnumUnionColumn(Fruit, Vegetable).process_result_value("quince", None)


def test_the_tolerant_column_answers_with_the_value_it_could_not_place() -> None:
    """A reader older than its writer, which under a rolling deploy is every reader for a minute."""
    column = TolerantTextBackedStrEnumUnionColumn(Fruit, Vegetable)

    assert column.process_result_value("quince", None) == UnknownValue("quince")


def test_tolerance_costs_the_known_members_nothing() -> None:
    column = TolerantTextBackedStrEnumUnionColumn(Fruit, Vegetable)

    assert [column.process_result_value(stored, None) for stored in ("apple", "leek", None)] == [
        Fruit.APPLE,
        Vegetable.LEEK,
        None,
    ]


def test_a_value_this_release_cannot_name_may_not_be_written_back() -> None:
    """Tolerance is for reading. Storing a value the writer cannot name would launder a vocabulary
    it has no words for into the database, which is a bug and not a state to carry."""
    column = TolerantTextBackedStrEnumUnionColumn(Fruit, Vegetable)

    with pytest.raises(ValueError, match="cannot name"):
        column.process_bind_param(UnknownValue("quince"), None)


def test_a_known_member_binds_as_its_stored_spelling() -> None:
    column = TolerantTextBackedStrEnumUnionColumn(Fruit, Vegetable)

    assert (column.process_bind_param(Fruit.PEAR, None), column.process_bind_param(None, None)) == ("pear", None)


if __name__ == "__main__":
    pytest_bazel.main()
