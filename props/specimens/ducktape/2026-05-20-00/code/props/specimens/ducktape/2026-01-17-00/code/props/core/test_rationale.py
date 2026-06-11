"""Tests for Rationale type validation."""

import pytest
from hamcrest import assert_that, equal_to
from pydantic import ValidationError

# Extract limits from the Rationale type's constraints
MIN_LENGTH = 10
MAX_LENGTH = 5000


def test_valid_rationales(rationale_model):
    """Valid rationales should be accepted."""
    # Minimum length
    m1 = rationale_model(rationale="1234567890")
    assert_that(m1.rationale, equal_to("1234567890"))

    # Normal rationale
    m2 = rationale_model(rationale="This is a valid rationale explaining the issue.")
    assert_that(len(m2.rationale), equal_to(47))

    # With newlines and tabs
    m3 = rationale_model(rationale="Line 1\nLine 2\n\tIndented")
    assert_that(m3.rationale, equal_to("Line 1\nLine 2\n\tIndented"))

    # Maximum length
    m4 = rationale_model(rationale="x" * MAX_LENGTH)
    assert_that(len(m4.rationale), equal_to(MAX_LENGTH))


def test_rejects_too_short(rationale_model):
    """Rationales under minimum length should be rejected."""
    with pytest.raises(ValidationError, match=f"at least {MIN_LENGTH} characters"):
        rationale_model(rationale="short")  # 5 chars

    with pytest.raises(ValidationError, match=f"at least {MIN_LENGTH} characters"):
        rationale_model(rationale="123456789")  # 9 chars


def test_rejects_too_long(rationale_model):
    """Rationales over maximum length should be rejected."""
    with pytest.raises(ValidationError, match=f"at most {MAX_LENGTH} characters"):
        rationale_model(rationale="x" * 50000)  # Ridiculously long


def test_strips_whitespace(rationale_model):
    """Whitespace should be stripped, and result must still meet min length."""
    # Leading/trailing whitespace stripped
    m1 = rationale_model(rationale="   1234567890   ")
    assert_that(m1.rationale, equal_to("1234567890"))

    # After stripping, must still meet minimum length
    with pytest.raises(ValidationError, match=f"at least {MIN_LENGTH} characters"):
        rationale_model(rationale="   short   ")  # Strips to "short" (5 chars)

    # Whitespace-only becomes empty after strip
    with pytest.raises(ValidationError, match=f"at least {MIN_LENGTH} characters"):
        rationale_model(rationale="   ")

    with pytest.raises(ValidationError, match=f"at least {MIN_LENGTH} characters"):
        rationale_model(rationale="\n\n\t\t")


def test_rejects_non_string(rationale_model):
    """Non-string values should be rejected."""
    with pytest.raises(ValidationError, match="valid string"):
        rationale_model(rationale=123)

    with pytest.raises(ValidationError, match="valid string"):
        rationale_model(rationale=None)
