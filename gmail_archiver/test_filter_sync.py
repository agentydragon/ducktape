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


def test_compound_from_condition_raises():
    """A compound `from` condition cannot be represented and must raise."""
    rule = FilterRule(from_=CompoundCondition(any=["a@example.com", "b@example.com"]))

    with pytest.raises(ValueError, match="from"):
        normalize_yaml_rule(rule)


def test_unrepresentable_cc_field_raises():
    """A criteria field the normalizer cannot represent (cc) must raise."""
    rule = FilterRule(cc="boss@example.com", trash=True)

    with pytest.raises(ValueError, match="cc"):
        normalize_yaml_rule(rule)


if __name__ == "__main__":
    pytest_bazel.main()
