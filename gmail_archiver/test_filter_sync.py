"""Tests for filter normalization and synchronization."""

import pytest
import pytest_bazel

from gmail_api.labels import SystemLabel
from gmail_archiver.filter_sync import NormalizedFilter, normalize_yaml_rule
from gmail_archiver.gmail_yaml_filters_models import CompoundCondition, FilterRule


def test_representable_rule_normalizes():
    """A rule using only representable criteria round-trips into NormalizedFilter."""
    rule = FilterRule(from_="alerts@example.com", subject="Receipt", has="invoice", does_not_have="draft", trash=True)

    normalized = normalize_yaml_rule(rule)

    assert normalized == NormalizedFilter(
        from_="alerts@example.com",
        subject="Receipt",
        query="invoice",
        negated_query="draft",
        add_labels=frozenset({SystemLabel.TRASH}),
    )


# Every criteria field NormalizedFilter cannot represent: setting any of them must
# fail closed rather than silently broaden the synced filter. Parameterized so a
# typo/omission in the reject list regresses a test. (attr, expected-name-in-error)
UNREPRESENTABLE_FIELDS = [
    ("match", "match"),
    ("missing", "missing"),
    ("no_match", "no_match"),
    ("bcc", "bcc"),
    ("cc", "cc"),
    ("list", "list"),
    ("labeled", "labeled"),
    ("is_", "is"),
    ("category", "category"),
    ("deliveredto", "deliveredto"),
    ("filename", "filename"),
    ("larger", "larger"),
    ("smaller", "smaller"),
    ("size", "size"),
    ("rfc822msgid", "rfc822msgid"),
]


@pytest.mark.parametrize(("attr", "error_name"), UNREPRESENTABLE_FIELDS)
def test_unrepresentable_field_raises(attr: str, error_name: str):
    rule = FilterRule(**{attr: "x"}, trash=True)

    with pytest.raises(ValueError, match=error_name):
        normalize_yaml_rule(rule)


# from/to/subject/has/does_not_have accept plain strings (representable) but also
# compound any/all/not conditions, which NormalizedFilter cannot carry.
COMPOUND_FIELDS = [
    ("from_", "from"),
    ("to", "to"),
    ("subject", "subject"),
    ("has", "has"),
    ("does_not_have", "does_not_have"),
]


@pytest.mark.parametrize(("attr", "error_name"), COMPOUND_FIELDS)
def test_compound_condition_raises(attr: str, error_name: str):
    rule = FilterRule(**{attr: CompoundCondition(any=["a@example.com", "b@example.com"])})

    with pytest.raises(ValueError, match=f"'{error_name}'"):
        normalize_yaml_rule(rule)


if __name__ == "__main__":
    pytest_bazel.main()
